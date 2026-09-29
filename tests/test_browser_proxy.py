"""Proxy boundary checks; optional real Chromium acceptance uses owned fixtures."""

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import socket
import socketserver
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from page_fetch import browser_proxy
from page_fetch import netguard


class BrowserProxyTests(unittest.TestCase):
    def test_cancelled_refused_requests_do_not_dump_tracebacks(self):
        original_connect = browser_proxy.connect_public
        original_process = browser_proxy._Proxy.process_request_thread
        for method, target in [("GET", "http://127.0.0.1/private"), ("CONNECT", "127.0.0.1:443")]:
            validating = threading.Event()
            cancelled = threading.Event()
            finished = threading.Event()
            errors = io.StringIO()

            def delayed_connect(url, timeout):
                validating.set()
                if not cancelled.wait(3):
                    raise AssertionError("fixture client did not cancel")
                return original_connect(url, timeout)

            def recorded_process(server, request, client_address):
                try:
                    return original_process(server, request, client_address)
                finally:
                    finished.set()

            with self.subTest(method=method), redirect_stderr(errors), \
                 patch.object(browser_proxy, "connect_public", side_effect=delayed_connect), \
                 patch.object(browser_proxy._Proxy, "process_request_thread", recorded_process), \
                 browser_proxy.public_browser_proxy() as proxy:
                port = int(proxy["server"].rsplit(":", 1)[1])
                with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                    client.sendall(f"{method} {target} HTTP/1.1\r\nHost: fixture\r\n\r\n".encode())
                    self.assertTrue(validating.wait(3))
                    # Reset the real client socket before the proxy writes its
                    # refusal, as Chromium does for cancelled background fetches.
                    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                cancelled.set()
                self.assertTrue(finished.wait(3))
            self.assertEqual(errors.getvalue(), "")

    def test_connect_tunnel_relays_bytes_to_the_validated_peer(self):
        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.sendall(self.request.recv(1024))

        peer = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        thread = threading.Thread(target=peer.serve_forever, daemon=True)
        thread.start()
        try:
            with browser_proxy.public_browser_proxy() as proxy, \
                 patch.object(netguard, "check_url", return_value=["127.0.0.1"]):
                proxy_port = int(proxy["server"].rsplit(":", 1)[1])
                with socket.create_connection(("127.0.0.1", proxy_port), timeout=3) as client:
                    client.sendall(f"CONNECT public.fixture:{peer.server_address[1]} HTTP/1.1\r\nHost: public.fixture\r\n\r\n".encode())
                    header = b""
                    while not header.endswith(b"\r\n\r\n"):
                        header += client.recv(1)
                    self.assertIn(b" 200 ", header)
                    client.sendall(b"synthetic tunnel bytes")
                    self.assertEqual(client.recv(1024), b"synthetic tunnel bytes")
        finally:
            peer.shutdown()
            peer.server_close()
            thread.join()

    def test_connection_uses_validated_numeric_address_without_second_resolution(self):
        with patch.object(netguard, "check_url", return_value=["93.184.216.34"]) as check, \
             patch.object(browser_proxy.socket, "socket") as make_socket:
            browser_proxy.connect_public("https://public.example/article", 5)
        check.assert_called_once_with("https://public.example/article")
        make_socket.return_value.connect.assert_called_once_with(("93.184.216.34", 443))

    def test_http_and_connect_refuse_private_destination_before_dial(self):
        with browser_proxy.public_browser_proxy() as proxy, \
             patch.object(browser_proxy.socket.socket, "connect") as connect:
            # Use a preconnected client so the mock observes only upstream dials.
            # The independent raw socket uses connect_ex for the owned proxy.
            for method, target in [("GET", "http://127.0.0.1/private"), ("CONNECT", "127.0.0.1:443")]:
                with self.subTest(method=method), socket.socket() as client:
                    port = int(proxy["server"].rsplit(":", 1)[1])
                    self.assertEqual(client.connect_ex(("127.0.0.1", port)), 0)
                    client.sendall(f"{method} {target} HTTP/1.1\r\nHost: fixture\r\n\r\n".encode())
                    self.assertIn(b" 403 ", client.recv(4096).split(b"\r\n", 1)[0])
            connect.assert_not_called()


