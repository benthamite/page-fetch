"""Find a free, licensed republication of a paywalled article.

Several paywalled outlets license their articles in full to partners that
publish them free: Bloomberg and WSJ stories on Yahoo Finance, Washington
Post stories in the Anchorage Daily News, FT stories in The Irish Times,
Economist (and some WSJ) stories in Mint. ``republished`` looks the article
up by headline on those partners and returns the copy, whose own URL is the
one to cite.

A copy is accepted only if its headline matches the one wanted and its page
credits the original outlet. Partners sometimes rewrite headlines, so a
miss does not mean no copy exists.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus, unquote, urlparse

import httpx

from .netguard import safe_client, safe_get
from .routes import DEFAULT_TIMEOUT, DEFAULT_USER_AGENT, FetchFailed, Fetched
from .text import page_title

MIN_SCORE = 0.6
MAX_CANDIDATES = 4

_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in into is it its of on or "
    "that the their to was were will with".split()
)
# Trailing " - Site", " | Site" or " – Site" parts of <title>.
_TITLE_SUFFIX = re.compile(r"\s+[-|–—]\s+[^-|–—]{1,40}$")


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower().replace("’", "'").replace("'", ""))
    return {w for w in words if w not in _STOPWORDS}


def title_score(wanted: str, candidate: str) -> float:
    """Dice overlap of the two headlines' content words, from 0 to 1."""
    a, b = _tokens(wanted), _tokens(candidate)
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def _strip_site_suffix(title: str) -> str:
    previous = None
    while previous != title:
        previous, title = title, _TITLE_SUFFIX.sub("", title).strip()
    return title


def title_from_url(url: str) -> str | None:
    """A headline guessed from the URL's slug, or None when it has none."""
    for segment in reversed([s for s in urlparse(url).path.split("/") if s]):
        segment = re.sub(r"\.html?$", "", segment)
        segment = re.sub(r"-[0-9a-f]{6,}$|-\d{6,}$", "", segment)
        words = [w for w in segment.split("-") if w]
        # A headline slug is mostly plain words; an ID (the FT's UUIDs) is not.
        plain = [w for w in words if w.isalpha()]
        if len(plain) >= 4 and len(plain) >= 0.6 * len(words):
            return " ".join(words)
    return None


# ── Finding candidates ────────────────────────────────────────────────

Candidate = tuple[str, str | None]  # (url, headline if the listing gives one)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def feed_entries(xml_text: str) -> list[Candidate]:
    """(url, headline) pairs from a news sitemap or an RSS feed; the headline
    is None for plain sitemap entries."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    entries: list[Candidate] = []
    for node in root.iter():
        name = _local(node.tag)
        if name not in ("url", "item"):
            continue
        fields = {}
        for child in node.iter():
            key = _local(child.tag)
            if key in ("loc", "link", "title") and key not in fields:
                fields[key] = (child.text or "").strip()
        link = fields.get("loc") or fields.get("link")
        if link:
            entries.append((link, fields.get("title") or None))
    return entries


def _slug_headline(url: str, headline: str | None) -> str | None:
    return headline or title_from_url(url)


def _get_text(client: httpx.Client, url: str) -> str:
    resp = safe_get(client, url)
    if resp.status_code != 200:
        raise FetchFailed(f"{url}: HTTP {resp.status_code}")
    return resp.text


def feed_finder(*feed_urls: str, path_filter: str | None = None):
    """Candidates whose listed (or slug) headline matches, from feeds."""
    def find(client: httpx.Client, wanted: str) -> list[Candidate]:
        scored = []
        for feed_url in feed_urls:
            try:
                entries = feed_entries(_get_text(client, feed_url))
            except (FetchFailed, httpx.HTTPError):
                continue
            for link, headline in entries:
                if path_filter and not re.search(path_filter, link):
                    continue
                guess = _slug_headline(link, headline)
                if guess:
                    scored.append((title_score(wanted, guess), link, headline))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [(link, headline) for score, link, headline in scored
                if score >= MIN_SCORE][:MAX_CANDIDATES]
    return find


def yahoo_news_finder(client: httpx.Client, wanted: str) -> list[Candidate]:
    """Article links from a Yahoo News search for the headline."""
    html = _get_text(client, "https://news.search.yahoo.com/search?p=" + quote_plus(wanted))
    links = []
    for encoded in re.findall(r"/RU=([^/]+)/R[KS]=", html):
        link = unquote(encoded).split("?")[0]
        host = urlparse(link).hostname or ""
        if host.endswith("yahoo.com") and "/articles/" in link and link not in links:
            links.append(link)
    return [(link, None) for link in links[:MAX_CANDIDATES]]


# ── Outlets and their partners ────────────────────────────────────────


@dataclass(frozen=True)
class Venue:
    name: str
    find: Callable[[httpx.Client, str], list[Candidate]]
    credit: re.Pattern  # must appear in the copy's HTML


_MINT_SITEMAPS = ("https://www.livemint.com/sitemap/today.xml",
                  "https://www.livemint.com/sitemap/yesterday.xml")

VENUES: dict[str, list[Venue]] = {
    "bloomberg.com": [
        Venue("Yahoo Finance", yahoo_news_finder, re.compile(r"\(Bloomberg\)|Bloomberg L\.P\.")),
    ],
    "wsj.com": [
        Venue("Yahoo Finance", yahoo_news_finder, re.compile(r"Wall Street Journal|Dow Jones")),
        Venue("Mint", feed_finder(*_MINT_SITEMAPS), re.compile(r"Wall Street Journal|\bWSJ\b")),
    ],
    "washingtonpost.com": [
        Venue("Anchorage Daily News",
              feed_finder("https://www.adn.com/arc/outboundfeeds/sitemap/"),
              re.compile(r"The Washington Post")),
    ],
    "ft.com": [
        Venue("The Irish Times",
              feed_finder("https://www.irishtimes.com/arc/outboundfeeds/sitemap-news-index/latest/"),
              re.compile(r"Financial Times Limited|The Financial Times")),
    ],
    "economist.com": [
        Venue("Mint", feed_finder(*_MINT_SITEMAPS, path_filter=r"/global/"),
              re.compile(r"The Economist")),
    ],
}


def _outlet(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for outlet in VENUES:
        if host == outlet or host.endswith("." + outlet):
            return outlet
    return None


def republished(url: str, *, title: str | None = None,
                timeout: float = DEFAULT_TIMEOUT,
                user_agent: str = DEFAULT_USER_AGENT) -> Fetched:
    """A licensed free copy of the article at ``url``.

    ``title`` is the article's headline; without it the headline is guessed
    from the URL, which fails for outlets (the FT) whose URLs carry none.
    The result's ``source_url`` is the copy, which is what to cite.
    """
    outlet = _outlet(url)
    if outlet is None:
        raise FetchFailed("republished: no known free republisher for this site")
    wanted = title or title_from_url(url)
    if not wanted:
        raise FetchFailed("republished: the URL has no headline; pass the title")
    tried = []
    with safe_client(timeout=timeout, headers={"User-Agent": user_agent}) as client:
        for venue in VENUES[outlet]:
            try:
                candidates = venue.find(client, wanted)
            except (FetchFailed, httpx.HTTPError) as e:
                tried.append(f"{venue.name}: search failed ({e})")
                continue
            for link, listed in candidates:
                try:
                    html = _get_text(client, link)
                except (FetchFailed, httpx.HTTPError):
                    continue
                shown = _strip_site_suffix(page_title(html) or "")
                score = max(title_score(wanted, shown),
                            title_score(wanted, listed) if listed else 0.0)
                if score >= MIN_SCORE and venue.credit.search(html):
                    return Fetched(url=url, source_url=link,
                                   route="republished", html=html)
            tried.append(f"{venue.name}: no matching copy")
    raise FetchFailed("republished: " + "; ".join(tried))
