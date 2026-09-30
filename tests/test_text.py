import unittest
from unittest import mock

from page_fetch import netguard, routes
from page_fetch import text as pf_text


class FetchFallbackTests(unittest.TestCase):
    def test_deep_url_redirect_to_homepage_is_not_usable_content(self):
        self.assertTrue(pf_text.redirect_loses_requested_path(
            "https://eea.europa.eu/en/analysis/publications/report",
            "https://www.eea.europa.eu/",
        ))

    def test_same_article_redirect_is_allowed(self):
        self.assertFalse(pf_text.redirect_loses_requested_path(
            "http://example.com/reports/story",
            "https://www.example.com/reports/story/",
        ))

    def test_www_variant_preserves_path_and_query(self):
        self.assertEqual(
            pf_text.www_url_variant("https://example.com/reports/story?a=1"),
            "https://www.example.com/reports/story?a=1",
        )

    def test_httpx_fetch_rejects_redirected_homepage_text(self):
        import httpx

        def handler(request):
            if request.url.host == "eea.europa.eu":
                return httpx.Response(
                    302, headers={"location": "https://www.eea.europa.eu/"}
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><body>European Environment Agency homepage</body></html>",
            )

        def fake_client(**kwargs):
            kwargs.pop("follow_redirects", None)
            return httpx.Client(
                transport=httpx.MockTransport(handler),
                follow_redirects=False,
                **kwargs,
            )

        with mock.patch.object(routes, "safe_client", fake_client), \
                mock.patch.object(
                    netguard, "resolve_public",
                    return_value=["93.184.216.34"],
                ):
            with self.assertRaisesRegex(routes.FetchFailed, "redirected away"):
                routes.direct(
                    "https://eea.europa.eu/en/analysis/publications/report"
                )

    def test_vercel_checkpoint_text_is_not_usable_content(self):
        text = (
            "Vercel Security Checkpoint We're verifying your browser "
            "Website owner? Click here to fix Enable JavaScript to continue"
        )

        self.assertIsNone(pf_text.usable_page_text(text))

    def test_cookie_consent_text_is_not_usable_content(self):
        text = (
            "PressReader.com - Digital Newspaper & Magazine Subscriptions "
            "Consent Details [#IABV2SETTINGS#] About This website uses cookies"
        )

        self.assertIsNone(pf_text.usable_page_text(text))

    def test_pressreader_subscription_text_is_not_usable_content(self):
        text = "PressReader.com - Digital Newspaper & Magazine Subscriptions"

        self.assertIsNone(pf_text.usable_page_text(text))

    def test_client_challenge_text_is_not_usable_content(self):
        text = (
            "Client Challenge JavaScript is disabled in your browser. "
            "Please enable JavaScript to proceed."
        )

        self.assertIsNone(pf_text.usable_page_text(text))

    def test_x_signed_out_wall_is_not_usable_content(self):
        text = (
            "X Log in Sign up We're unable to show this content The content "
            "may be private, deleted or only available on the app. "
            "Hmm...this page doesn't exist. Try searching for something "
            "else. Search Log in or sign up for X See what's happening and "
            "join the conversation Continue with phone Continue with Apple "
            "Terms Privacy Cookies Scan to get the app"
        )

        self.assertIsNone(pf_text.usable_page_text(text))

    def test_nginx_placeholder_text_is_not_usable_content(self):
        text = (
            "Welcome to nginx! If you see this page, the nginx web server "
            "is successfully installed and working. Further configuration "
            "is required."
        )

        self.assertIsNone(pf_text.usable_page_text(text))

    def test_archive_captcha_text_is_not_usable_content(self):
        text = (
            "archive.ph One more step Please complete the security check "
            "to access archive.ph Why do I have to complete a CAPTCHA?"
        )

        self.assertIsNone(pf_text.usable_page_text(text))


class ArchiveTodayHostTests(unittest.TestCase):
    def _run(self, pages):
        import httpx

        def handler(request):
            body = pages.get(request.url.host)
            if body is None:
                return httpx.Response(503)
            return httpx.Response(200, headers={"content-type": "text/html"}, text=body)

        def fake_client(**kwargs):
            kwargs.pop("follow_redirects", None)
            return httpx.Client(transport=httpx.MockTransport(handler),
                                follow_redirects=False, **kwargs)

        with mock.patch.object(routes, "safe_client", fake_client), \
                mock.patch.object(netguard, "resolve_public", return_value=["93.184.216.34"]):
            return routes.archive_today("https://www.example.com/a/story")

    def test_walled_first_host_falls_through_to_next(self):
        wall = ("archive.ph One more step Please complete the security check "
                "to access archive.ph Why do I have to complete a CAPTCHA?")
        page = self._run({"archive.ph": wall, "archive.is": "<p>The full article text.</p>"})
        self.assertIn("archive.is", page.source_url)

    def test_all_hosts_failing_lists_each_reason(self):
        with self.assertRaisesRegex(routes.FetchFailed,
                                    "archive.ph: HTTP 503; archive.is: HTTP 503"):
            self._run({})
