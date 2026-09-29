"""Connection-level public-address proxy for the Chromium fetcher.

HTTP destinations and HTTPS CONNECT authorities are resolved once and dialed
by validated numeric address. Redirects, popups, workers and extensions use
the same proxy. HTTPS remains end-to-end TLS: no certificate interception.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import select
import socket
import threading
from urllib.parse import urlsplit, urlunsplit

from . import netguard


def connect_public(url: str, timeout: float) -> socket.socket:
    addresses = netguard.check_url(url)
    port = netguard.url_port(url)
    last_error = None
    for address in addresses:
        # The socket receives an IP, never a hostname it could resolve again.
        family = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        try:
            connection.connect((address, port))
            return connection
        except OSError as error:
            connection.close()
            last_error = error
    raise last_error or OSError("No validated destination")


class _Proxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, timeout):
        self.connection_timeout = timeout
        self.connections = set()
        self.connection_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)

    def track(self, connection):
        with self.connection_lock:
            self.connections.add(connection)

    def untrack(self, connection):
        with self.connection_lock:
            self.connections.discard(connection)

    def close_connections(self):
        with self.connection_lock:
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()


class _Handler(BaseHTTPRequestHandler):
    # Do not prefetch request-body/TLS bytes before handing the socket to relay.
    rbufsize = 0
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.connection_timeout)
        self.server.track(self.connection)

    def finish(self):
        self.server.untrack(self.connection)
        super().finish()

    def log_message(self, *_args):
        # Request URLs/headers may contain private publisher credentials.
        pass

    def send_error(self, code, message=None, explain=None):
        try:
            super().send_error(code, message, explain)
        except (BrokenPipeError, ConnectionResetError):
            # Chromium may cancel while a destination is being refused. A
            # disconnected client cannot receive the response; other faults
            # still propagate through the server's normal error reporting.
            self.close_connection = True

    def _forward(self, *, tunnel):
        upstream = None
        self.close_connection = True
        try:
            url = "https://" + self.path + "/" if tunnel else self.path
            parsed = urlsplit(url)
            if parsed.username is not None or parsed.password is not None:
                raise netguard.UnsafeURL("Proxy URL credentials are not permitted")
            if tunnel:
                if parsed.path != "/" or parsed.query or parsed.fragment or parsed.port is None:
                    raise netguard.UnsafeURL("Invalid CONNECT authority")
            elif parsed.scheme != "http" or parsed.fragment:
                raise netguard.UnsafeURL("Expected an absolute HTTP URL")
            upstream = connect_public(url, self.server.connection_timeout)
            self.server.track(upstream)
        except (netguard.UnsafeURL, ValueError):
            self.send_error(403, "Destination refused")
            return
        except OSError:
            self.send_error(502, "Destination connection failed")
            return
        try:
            if tunnel:
                self.send_response(200, "Connection established")
                self.end_headers()
                self.wfile.flush()
            else:
                target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                headers = [(name, value) for name, value in self.headers.items()
                           if name.lower() not in {"host", "connection", "proxy-connection", "proxy-authorization"}]
                headers.extend([("Host", parsed.netloc), ("Connection", "close")])
                request = f"{self.command} {target} HTTP/1.1\r\n"
                request += "".join(f"{name}: {value}\r\n" for name, value in headers) + "\r\n"
                upstream.sendall(request.encode("latin-1"))
            self._relay(upstream)
        except OSError:
            # The browser owns request retry/error reporting; never dump a URL.
            pass
        finally:
            self.server.untrack(upstream)
            upstream.close()

    def _relay(self, upstream):
        sockets = [self.connection, upstream]
        while True:
            readable, _, _ = select.select(sockets, [], [], self.server.connection_timeout)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                target = upstream if source is self.connection else self.connection
                target.sendall(data)

    def do_CONNECT(self):
        self._forward(tunnel=True)

    def do_GET(self):
        self._forward(tunnel=False)

    do_HEAD = do_GET
    do_POST = do_GET
    do_PUT = do_GET
    do_PATCH = do_GET
    do_DELETE = do_GET
    do_OPTIONS = do_GET


@contextmanager
def public_browser_proxy(timeout: float = 30):
    server = _Proxy(timeout)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        yield {"server": f"http://127.0.0.1:{server.server_port}", "bypass": "<-loopback>"}
    finally:
        server.shutdown()
        server.close_connections()
        server.server_close()
        thread.join()
