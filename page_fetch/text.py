"""Helpers for judging and flattening fetched HTML."""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import urlparse


def html_to_text(html: str) -> str:
    """Crude HTML to text conversion — strip tags, collapse whitespace."""
    # Remove script and style blocks.
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.I)
    # Remove tags.
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common entities.
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(entity, char)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def page_title(html: str) -> str | None:
    """The page's <title>, whitespace-collapsed, or None."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.DOTALL | re.I)
    if not match:
        return None
    title = re.sub(r"\s+", " ", html_lib.unescape(match.group(1))).strip()
    return title or None


_INTERSTITIAL_PATTERNS = (
    ("vercel security checkpoint",),
    ("we're verifying your browser",),
    ("enable javascript to continue", "security checkpoint"),
    ("website owner? click here to fix",),
    ("checking if the site connection is secure",),
    ("checking your browser before accessing",),
    ("verify you are human",),
    ("verifying you are human",),
    ("just a moment", "cloudflare"),
    ("access denied", "reference #"),
    ("client challenge", "javascript is disabled"),
    ("consent details [#iabv2settings#]", "this website uses cookies"),
    ("pressreader.com - digital newspaper", "magazine subscriptions"),
    ("one more step", "complete the security check", "captcha"),
    ("welcome to nginx", "further configuration is required"),
    ("javascript is not available", "enable javascript"),
    # X's signed-out walls. Rendered by a browser, these run ~500 chars,
    # long enough to pass a length check as an article.
    ("unable to show this content",),
    ("log in or sign up for x", "scan to get the app"),
)


def looks_like_interstitial(text: str | None) -> bool:
    """Return True when extracted text is an access wall, not article text."""
    if not text:
        return False
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return any(
        all(fragment in normalized for fragment in pattern)
        for pattern in _INTERSTITIAL_PATTERNS
    )


def usable_page_text(text: str | None) -> str | None:
    """Normalize fetched text and drop access/interstitial pages."""
    if not text:
        return None
    text = text.strip()
    if not text or looks_like_interstitial(text):
        return None
    return text


def _path_segments(url: str) -> list[str]:
    """Return non-empty path segments for redirect sanity checks."""
    return [part for part in urlparse(url).path.split("/") if part]


def redirect_loses_requested_path(requested_url: str, final_url: str) -> bool:
    """True when a deep URL redirected to an obvious homepage/listing."""
    requested_segments = _path_segments(requested_url)
    final_segments = _path_segments(final_url)
    if not requested_segments:
        return False
    if not final_segments:
        return True
    if (
        len(requested_segments) >= 2
        and len(final_segments) <= 1
        and requested_segments != final_segments
    ):
        return True
    return False


def www_url_variant(url: str) -> str | None:
    """Return the equivalent URL with a www. host, when applicable."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme not in ("http", "https"):
        return None
    if not host or host.startswith("www.") or "." not in host:
        return None
    return parsed._replace(netloc=f"www.{parsed.netloc}").geturl()
