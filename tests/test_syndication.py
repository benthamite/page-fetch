import unittest
from unittest import mock
from urllib.parse import quote

import httpx

from page_fetch import ROUTES, FetchFailed, netguard
from page_fetch import syndication as rp

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.adn.com/nation-world/2026/09/29/unrelated-story-about-salmon-runs/</loc></url>
  <url><loc>https://www.adn.com/nation-world/2026/09/29/hegseth-bars-military-academies-from-hiring-civilians-as-tenured-professors/</loc></url>
</urlset>"""

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title><![CDATA[Bardella takes legal action over allegations]]></title>
        <link>https://www.irishtimes.com/world/2026/09/29/b/</link></item>
</channel></rss>"""

ARTICLE = ("<html><head><title>Hegseth bars military academies from hiring civilians "
           "as tenured professors - Anchorage Daily News</title></head>"
           "<body><p>By Todd Wallack, The Washington Post</p></body></html>")


def _client_for(pages):
    def handler(request):
        body = pages.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, headers={"content-type": "text/html"}, text=body)

    def fake_client(**kwargs):
        kwargs.pop("follow_redirects", None)
        return httpx.Client(transport=httpx.MockTransport(handler),
                            follow_redirects=False, **kwargs)
    return fake_client


class TitleTests(unittest.TestCase):
    def test_score_ignores_case_punctuation_and_stopwords(self):
        self.assertEqual(rp.title_score("The Fed’s rate cut", "fed's RATE cut!"), 1.0)
        self.assertLess(rp.title_score("Fed cuts rates", "Oil prices fall"), 0.1)

    def test_title_from_url_uses_slug_and_drops_ids(self):
        self.assertEqual(
            rp.title_from_url("https://www.livemint.com/global/big-law-is-being-shaken-up-11790669695362.html"),
            "big law is being shaken up")
        self.assertIsNone(rp.title_from_url("https://www.ft.com/content/47019489-f00e-4c96-bb79-5c628c89b3a1"))

    def test_strip_site_suffix(self):
        self.assertEqual(rp._strip_site_suffix("Big law is shaken up | Mint"), "Big law is shaken up")
        self.assertEqual(rp._strip_site_suffix("A story – The Irish Times"), "A story")


class FeedTests(unittest.TestCase):
    def test_sitemap_and_rss_entries(self):
        self.assertEqual(len(rp.feed_entries(SITEMAP)), 2)
        self.assertEqual(rp.feed_entries(RSS), [
            ("https://www.irishtimes.com/world/2026/09/29/b/",
             "Bardella takes legal action over allegations")])
        self.assertEqual(rp.feed_entries("not xml"), [])


class RepublishedTests(unittest.TestCase):
    WAPO = "https://www.washingtonpost.com/national-security/2026/09/29/x/"
    COPY = "https://www.adn.com/nation-world/2026/09/29/hegseth-bars-military-academies-from-hiring-civilians-as-tenured-professors/"
    TITLE = "Hegseth bars military academies from hiring civilians as tenured professors"

    def _run(self, pages, **kwargs):
        with mock.patch.object(rp, "safe_client", _client_for(pages)), \
                mock.patch.object(netguard, "resolve_public", return_value=["93.184.216.34"]):
            return rp.republished(self.WAPO, **kwargs)

    def test_finds_credited_copy_with_matching_headline(self):
        pages = {"https://www.adn.com/arc/outboundfeeds/sitemap/": SITEMAP, self.COPY: ARTICLE}
        page = self._run(pages, title=self.TITLE)
        self.assertEqual(page.source_url, self.COPY)
        self.assertEqual(page.url, self.WAPO)
        self.assertEqual(page.route, "republished")

    def test_rejects_copy_without_credit(self):
        pages = {"https://www.adn.com/arc/outboundfeeds/sitemap/": SITEMAP,
                 self.COPY: ARTICLE.replace("The Washington Post", "Staff")}
        with self.assertRaisesRegex(FetchFailed, "no matching copy"):
            self._run(pages, title=self.TITLE)

    def test_rejects_unrelated_headline(self):
        pages = {"https://www.adn.com/arc/outboundfeeds/sitemap/": SITEMAP, self.COPY: ARTICLE}
        with self.assertRaisesRegex(FetchFailed, "no matching copy"):
            self._run(pages, title="Navy admiral fired over carrier dispute")

    def test_rejects_copy_listed_off_the_partner_domain(self):
        offsite = self.COPY.replace("www.adn.com", "adn.com.example.net")
        pages = {"https://www.adn.com/arc/outboundfeeds/sitemap/": SITEMAP.replace(self.COPY, offsite),
                 offsite: ARTICLE}
        with self.assertRaisesRegex(FetchFailed, "no matching copy"):
            self._run(pages, title=self.TITLE)

    def test_rejects_copy_that_redirects_off_the_partner_domain(self):
        elsewhere = "https://example.net/copy"
        pages = {"https://www.adn.com/arc/outboundfeeds/sitemap/": SITEMAP,
                 self.COPY: httpx.Response(302, headers={"location": elsewhere}),
                 elsewhere: ARTICLE}
        with self.assertRaisesRegex(FetchFailed, "no matching copy"):
            self._run(pages, title=self.TITLE)

    def test_yahoo_search_keeps_only_yahoo_hosts(self):
        def result(link):
            return f'<a href="https://r.search.yahoo.com/_ylt=x/RU={quote(link, safe="")}/RK=2/RS=y">'
        real = "https://finance.yahoo.com/news/articles/fed-cuts-rates-123.html"
        html = "".join(result(link) for link in (
            "https://evilyahoo.com/news/articles/fed-cuts-rates.html",
            "https://finance.yahoo.com.example.net/articles/fed-cuts-rates.html",
            real))
        pages = {"https://news.search.yahoo.com/search?p=fed+cuts+rates": html}
        with mock.patch.object(netguard, "resolve_public", return_value=["93.184.216.34"]), \
                _client_for(pages)() as client:
            self.assertEqual(rp.yahoo_news_finder(client, "fed cuts rates"), [(real, None)])

    def test_wsj_credit_on_yahoo_needs_the_journal_as_provider(self):
        credit = rp.VENUES["wsj.com"][0].credit
        # Every Yahoo Finance page has "Dow Jones" in its ticker, and other
        # outlets' stories cite the Journal in their text.
        other = ('<a href="/quote/%5EDJI/">Dow Jones</a><p>The Wall Street Journal reported</p>'
                 '<script>{"provider":{"@type":"Organization","name":"Moneywise"}}</script>')
        wsj = '<script>{"provider":{"@type":"Organization","name":"The Wall Street Journal","url":"http://www.wsj.com/"}}</script>'
        self.assertIsNone(credit.search(other))
        self.assertIsNotNone(credit.search(wsj))

    def test_unknown_site_and_missing_headline(self):
        with self.assertRaisesRegex(FetchFailed, "no known free republisher"):
            rp.republished("https://www.nytimes.com/2026/09/27/a-story-with-a-long-slug.html")
        with self.assertRaisesRegex(FetchFailed, "pass the title"):
            rp.republished("https://www.ft.com/content/47019489-f00e-4c96-bb79-5c628c89b3a1")

    def test_registered_as_route(self):
        self.assertIs(ROUTES["republished"], rp.republished)


if __name__ == "__main__":
    unittest.main()
