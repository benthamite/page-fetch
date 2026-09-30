"""Ways of getting a page's HTML, from a plain request to archived copies.

Each route takes the URL of the page wanted and returns a ``Fetched`` or
raises ``FetchFailed`` with a one-line reason. Routes never fall back to
one another: callers choose the order, so each automation can decide how
far to go and say which route produced the text it uses.

Every request goes through ``netguard``, so a route can only reach public
addresses, including on redirects.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlparse

import httpx

from . import netguard
from .netguard import safe_client, safe_get
from .text import (
    html_to_text,
    page_title,
    redirect_loses_requested_path,
    usable_page_text,
    www_url_variant,
)

DEFAULT_TIMEOUT = 15
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
# A Googlebot address; some paywalls also key on the client address.
GOOGLEBOT_IP = "66.249.66.1"


class FetchFailed(Exception):
    """A route could not produce the page; the message says why."""


@dataclass(frozen=True)
class Fetched:
    """A page's HTML and where it actually came from.

    ``url`` is the page wanted; ``source_url`` is the address that served
    the HTML (after redirects, or the archive snapshot), so callers can cite
    ``url`` while telling readers what was read.
    """

    url: str
    source_url: str
    route: str
    html: str

    @property
    def title(self) -> str | None:
        return page_title(self.html)

    @property
    def text(self) -> str | None:
        """Tag-stripped text, or None when it is empty or an access wall."""
        return usable_page_text(html_to_text(self.html))


def _check_page_response(url: str, resp) -> None:
    if resp.status_code != 200:
        raise FetchFailed(f"HTTP {resp.status_code}")
    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        raise FetchFailed(f"not a web page ({content_type or 'no content type'})")


def _get_page(url: str, route: str, *, timeout: float, headers: dict) -> Fetched:
    try:
        with safe_client(timeout=timeout, headers=headers) as client:
            resp = safe_get(client, url)
            _check_page_response(url, resp)
            final = str(resp.url)
            if redirect_loses_requested_path(url, final):
                raise FetchFailed(f"redirected away from the article to {final}")
            return Fetched(url=url, source_url=final, route=route, html=resp.text)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        raise FetchFailed(f"{type(e).__name__}: {e}") from e


def direct(url: str, *, timeout: float = DEFAULT_TIMEOUT,
           user_agent: str = DEFAULT_USER_AGENT) -> Fetched:
    """A plain request with a browser user agent."""
    return _get_page(url, "direct", timeout=timeout,
                     headers={"User-Agent": user_agent})


def www_variant(url: str, *, timeout: float = DEFAULT_TIMEOUT,
                user_agent: str = DEFAULT_USER_AGENT) -> Fetched:
    """A plain request to the www. host, for sites whose bare host
    redirects articles to the homepage."""
    variant = www_url_variant(url)
    if not variant:
        raise FetchFailed("no www. variant of this URL")
    fetched = _get_page(variant, "www-variant", timeout=timeout,
                        headers={"User-Agent": user_agent})
    return Fetched(url=url, source_url=fetched.source_url,
                   route=fetched.route, html=fetched.html)


def google_referer(url: str, *, timeout: float = DEFAULT_TIMEOUT,
                   user_agent: str = DEFAULT_USER_AGENT) -> Fetched:
    """A request that looks like a click from Google search, which some
    paywalls (the FT among them) answer with the full article."""
    return _get_page(url, "google-referer", timeout=timeout, headers={
        "User-Agent": user_agent,
        "Referer": "https://www.google.com/",
        "X-Forwarded-For": GOOGLEBOT_IP,
    })


def impersonate(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> Fetched:
    """A request with a real-browser TLS fingerprint via curl_cffi.

    Some WAFs (Cloudflare, Vercel) refuse plain Python HTTP clients on
    their TLS/JA3 fingerprint alone, regardless of headers. Needs the
    ``impersonate`` extra.
    """
    try:
        from curl_cffi import CurlOpt
        from curl_cffi import requests as curl_requests
    except ImportError as e:
        raise FetchFailed("curl_cffi is not installed") from e
    try:
        # Redirects are followed by hand so every hop is validated, and each
        # hop's connection is pinned to the validated addresses with
        # CURLOPT_RESOLVE so the host cannot re-resolve to a private one.
        current = url
        for _ in range(netguard.MAX_REDIRECTS + 1):
            addresses = netguard.check_url(current)
            host = urlparse(current).hostname or ""
            port = netguard.url_port(current)
            pin = [f"{host}:{port}:{','.join(addresses)}"]
            with curl_requests.Session(
                curl_options={CurlOpt.RESOLVE: pin}
            ) as session:
                resp = session.get(
                    current,
                    impersonate="chrome",
                    timeout=timeout,
                    allow_redirects=False,
                )
            location = resp.headers.get("location", "")
            if resp.status_code in (301, 302, 303, 307, 308) and location:
                current = urljoin(current, location)
                continue
            break
        else:
            raise FetchFailed("too many redirects")
        _check_page_response(url, resp)
        if redirect_loses_requested_path(url, current):
            raise FetchFailed(f"redirected away from the article to {current}")
        return Fetched(url=url, source_url=current, route="impersonate",
                       html=resp.text)
    except FetchFailed:
        raise
    except Exception as e:
        raise FetchFailed(f"{type(e).__name__}: {e}") from e


def _long_enough(fetched: Fetched, min_text_len: int) -> bool:
    text = fetched.text
    return bool(text) and len(text) >= min_text_len


# archive.today answers on several domains, sometimes on different servers;
# when one is unreachable another usually is.
ARCHIVE_TODAY_HOSTS = ("archive.ph", "archive.is")


def archive_today(url: str, *, timeout: float = DEFAULT_TIMEOUT,
                  user_agent: str = DEFAULT_USER_AGENT,
                  min_text_len: int = 0,
                  hosts: tuple[str, ...] = ARCHIVE_TODAY_HOSTS) -> Fetched:
    """The newest archive.today snapshot of the page, trying each of
    ``hosts`` until one answers.

    Snapshots often hold the full text of paywalled articles. They are made
    by whoever asked for them, so check ``title`` against the article
    wanted: a snapshot can be of a different page at a similar address.
    """
    failures = []
    for host in hosts:
        archive_url = f"https://{host}/newest/{quote(url, safe=':/')}"
        try:
            with safe_client(timeout=timeout,
                             headers={"User-Agent": user_agent}) as client:
                resp = safe_get(client, archive_url)
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            failures.append(f"{host}: {type(e).__name__}")
            continue
        if resp.status_code != 200:
            failures.append(f"{host}: HTTP {resp.status_code}")
            continue
        fetched = Fetched(url=url, source_url=str(resp.url),
                          route="archive.today", html=resp.text)
        # A 200 carrying a CAPTCHA or other wall is archive.today's commonest
        # failure, and another host may still serve the snapshot.
        if not _long_enough(fetched, min_text_len):
            failures.append(f"{host}: no usable snapshot text")
            continue
        return fetched
    raise FetchFailed("archive.today: " + "; ".join(failures))


def wayback(url: str, *, timeout: float = DEFAULT_TIMEOUT,
            min_text_len: int = 0) -> Fetched:
    """The Wayback Machine's closest snapshot of the page, trying the URL
    as given and then without its query string."""
    candidates = [url]
    parsed = urlparse(url)
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if clean != url:
        candidates.append(clean)

    try:
        with safe_client(timeout=timeout) as client:
            for candidate in candidates:
                resp = safe_get(
                    client,
                    "https://archive.org/wayback/available",
                    params={"url": candidate},
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                snap = (data.get("archived_snapshots") or {}).get("closest")
                if not snap or not snap.get("available"):
                    continue

                wb_url = snap["url"]
                resp = safe_get(client, wb_url)
                if resp.status_code != 200:
                    continue
                fetched = Fetched(url=url, source_url=str(resp.url),
                                  route="wayback", html=resp.text)
                if _long_enough(fetched, min_text_len):
                    return fetched
    except Exception as e:
        raise FetchFailed(f"wayback: {type(e).__name__}: {e}") from e
    raise FetchFailed("wayback: no usable snapshot")


ROUTES = {
    "direct": direct,
    "impersonate": impersonate,
    "www-variant": www_variant,
    "google-referer": google_referer,
    "archive.today": archive_today,
    "wayback": wayback,
}
