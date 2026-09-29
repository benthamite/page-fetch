"""Regression tests for the outbound-fetch guard (SSRF / DNS rebinding)."""

import socket
import sys
import tempfile
import types
import unittest
from unittest import mock

import httpcore
import httpx
from httpcore._backends.mock import MockStream

from page_fetch import netguard, routes

PUBLIC_IP = "93.184.216.34"


def _addrinfo(*addresses):
    return [
        (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
        for a in addresses
    ]


class ResolvePublicTests(unittest.TestCase):
    def test_literal_loopback_rejected(self):
        with self.assertRaises(netguard.UnsafeURL):
            netguard.resolve_public("127.0.0.1")
        with self.assertRaises(netguard.UnsafeURL):
            netguard.resolve_public("::1")

    def test_link_local_metadata_address_rejected(self):
        with self.assertRaises(netguard.UnsafeURL):
            netguard.resolve_public("169.254.169.254")

    def test_ipv4_mapped_loopback_rejected(self):
        with self.assertRaises(netguard.UnsafeURL):
            netguard.resolve_public("::ffff:127.0.0.1")
        with self.assertRaises(netguard.UnsafeURL):
            netguard.resolve_public("[::ffff:10.0.0.5]")

    def test_other_non_public_ranges_rejected(self):
        for address in ("10.1.2.3", "192.168.1.1", "172.16.0.1", "0.0.0.0",
                        "100.64.0.1", "224.0.0.1", "240.0.0.1", "fe80::1",
                        "fc00::1", "::"):
            with self.subTest(address=address):
                with self.assertRaises(netguard.UnsafeURL):
                    netguard.resolve_public(address)

    def test_public_literal_accepted(self):
        self.assertEqual(netguard.resolve_public(PUBLIC_IP), [PUBLIC_IP])
        self.assertEqual(
            netguard.resolve_public("2606:2800:220:1:248:1893:25c8:1946"),
            ["2606:2800:220:1:248:1893:25c8:1946"],
        )

    def test_mixed_public_and_private_resolution_rejects_host(self):
        with mock.patch.object(
            socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP, "10.0.0.8")
        ):
            with self.assertRaisesRegex(netguard.UnsafeURL, "10.0.0.8"):
                netguard.resolve_public("rebind.example")

    def test_all_public_resolution_returns_unique_addresses(self):
        with mock.patch.object(
            socket, "getaddrinfo",
            return_value=_addrinfo(PUBLIC_IP, PUBLIC_IP, "2606:2800:220:1:248:1893:25c8:1946"),
        ):
            self.assertEqual(
                netguard.resolve_public("public.example"),
                [PUBLIC_IP, "2606:2800:220:1:248:1893:25c8:1946"],
            )

    def test_unresolvable_host_rejected(self):
        with mock.patch.object(
            socket, "getaddrinfo", side_effect=socket.gaierror("nope")
        ):
            with self.assertRaises(netguard.UnsafeURL):
                netguard.resolve_public("missing.example")


class UrlIsSafeTests(unittest.TestCase):
    def test_rejects_schemes_ports_and_private_hosts(self):
        for url in ("ftp://example.com/", "file:///etc/passwd",
                    "http://example.com:22/", "http://127.0.0.1:8788/",
                    "http://[::ffff:127.0.0.1]/", "http://169.254.169.254/",
                    "http:///path", ""):
            with self.subTest(url=url):
                self.assertFalse(netguard.url_is_safe(url))

    def test_accepts_public_host_on_allowed_port(self):
        with mock.patch.object(
            netguard, "resolve_public", return_value=[PUBLIC_IP]
        ):
            self.assertTrue(netguard.url_is_safe("https://public.example/a"))
            self.assertTrue(netguard.url_is_safe("http://public.example:8080/a"))


class SafeTransportTests(unittest.TestCase):
    def test_backend_connects_to_validated_address(self):
        seen = {}

        def fake_connect(self, host, port, timeout=None, local_address=None,
                         socket_options=None):
            seen["target"] = (host, port)
            return MockStream([
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
            ])

        with mock.patch.object(
            socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)
        ), mock.patch.object(httpcore.SyncBackend, "connect_tcp", fake_connect):
            with netguard.safe_client() as client:
                resp = netguard.safe_get(client, "http://public.example/page")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "ok")
        self.assertEqual(seen["target"], (PUBLIC_IP, 80))
        self.assertEqual(resp.request.headers["host"], "public.example")

    def test_backend_refuses_host_resolving_to_loopback(self):
        connected = []

        def fake_connect(self, *args, **kwargs):
            connected.append(args)
            raise AssertionError("must not connect")

        with mock.patch.object(
            socket, "getaddrinfo", return_value=_addrinfo("127.0.0.1")
        ), mock.patch.object(httpcore.SyncBackend, "connect_tcp", fake_connect):
            with netguard.safe_client() as client:
                # Bypass safe_get's own check to prove the transport refuses
                # on its own when the name re-resolves to loopback.
                with self.assertRaises(httpx.ConnectError):
                    client.get("http://rebind.example/")
        self.assertEqual(connected, [])

    def test_safe_client_forces_redirects_off_and_refuses_proxies(self):
        with netguard.safe_client(follow_redirects=True) as client:
            self.assertFalse(client.follow_redirects)
            self.assertIsInstance(client._transport, netguard.SafeTransport)
        with self.assertRaises(ValueError):
            netguard.safe_client(proxy="http://proxy.example:3128")


class SafeGetRedirectTests(unittest.TestCase):
    def _client(self, handler):
        return httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )

    def test_redirect_to_loopback_stopped_before_second_hop(self):
        requested = []

        def handler(request):
            requested.append(str(request.url))
            return httpx.Response(
                302, headers={"location": "http://127.0.0.1:8788/admin"}
            )

        with mock.patch.object(
            socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)
        ):
            with self._client(handler) as client:
                with self.assertRaisesRegex(netguard.UnsafeURL, "port 8788"):
                    netguard.safe_get(client, "https://public.example/start")

        self.assertEqual(requested, ["https://public.example/start"])

    def test_redirect_to_private_host_on_allowed_port_stopped(self):
        requested = []

        def handler(request):
            requested.append(str(request.url))
            return httpx.Response(
                301, headers={"location": "http://169.254.169.254/latest/"}
            )

        with mock.patch.object(
            socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)
        ):
            with self._client(handler) as client:
                with self.assertRaises(netguard.UnsafeURL):
                    netguard.safe_get(client, "https://public.example/start")

        self.assertEqual(requested, ["https://public.example/start"])

    def test_public_redirect_chain_is_followed_with_history(self):
        def handler(request):
            if request.url.path == "/start":
                return httpx.Response(
                    301, headers={"location": "https://public.example/end"}
                )
            return httpx.Response(200, text="done")

        with mock.patch.object(
            socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)
        ):
            with self._client(handler) as client:
                resp = netguard.safe_get(client, "https://public.example/start")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(str(resp.url), "https://public.example/end")
        self.assertEqual([r.status_code for r in resp.history], [301])

    def test_redirect_loop_raises_too_many_redirects(self):
        def handler(request):
            return httpx.Response(
                302, headers={"location": "https://public.example/loop"}
            )

        with mock.patch.object(
            socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)
        ):
            with self._client(handler) as client:
                with self.assertRaises(httpx.TooManyRedirects):
                    netguard.safe_get(
                        client, "https://public.example/loop", max_redirects=3
                    )


class ImpersonateRouteTests(unittest.TestCase):
    def test_impersonate_pins_resolution_and_stops_at_private_redirect(self):
        calls = []

        class FakeResponse:
            def __init__(self, status_code, headers, text=""):
                self.status_code = status_code
                self.headers = headers
                self.text = text

        class FakeSession:
            def __init__(self, curl_options=None):
                self.curl_options = curl_options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, **kwargs):
                calls.append((url, self.curl_options, kwargs))
                return FakeResponse(
                    302, {"location": "http://127.0.0.1:8788/"}
                )

        fake_curl = types.SimpleNamespace(
            CurlOpt=types.SimpleNamespace(RESOLVE=10203),
            requests=types.SimpleNamespace(Session=FakeSession),
        )
        with mock.patch.dict(sys.modules, {
            "curl_cffi": fake_curl, "curl_cffi.requests": fake_curl.requests,
        }), mock.patch.object(
            socket, "getaddrinfo", return_value=_addrinfo(PUBLIC_IP)
        ):
            with self.assertRaises(routes.FetchFailed):
                routes.impersonate("https://public.example/a")

        self.assertEqual(len(calls), 1)
        url, curl_options, kwargs = calls[0]
        self.assertEqual(url, "https://public.example/a")
        self.assertEqual(
            curl_options, {10203: [f"public.example:443:{PUBLIC_IP}"]}
        )
        self.assertFalse(kwargs["allow_redirects"])
