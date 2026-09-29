"""Outbound-fetch guard against SSRF and DNS rebinding.

Every URL the pipeline fetches is attacker-influenced (Slack posts, Google
Alerts, Ahrefs backlinks, links scraped from fetched pages), and the pipeline
runs on a machine with local services on loopback and a 1Password token in
its environment. This module makes sure a fetch can only reach a public
address:

* ``resolve_public(host)`` resolves a hostname and rejects the whole host if
  *any* returned address is private, loopback, link-local, reserved,
  multicast, unspecified, or an IPv4-mapped IPv6 address wrapping one.
* ``check_url``/``url_is_safe`` require http(s), an allowed port, and a host
  that resolves only to public addresses.
* ``SafeTransport`` pins httpx connections to the validated address: the
  connection pool's network backend resolves and validates inside
  ``connect_tcp`` and connects to that IP, so a host cannot re-resolve to a
  private address between the check and the connect (DNS rebinding). TLS
  still uses the hostname for SNI and certificate verification because the
  request origin is left untouched.
* ``safe_get`` follows redirects by hand, validating each hop.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpcore
import httpx

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
MAX_REDIRECTS = 5


class UnsafeURL(httpx.ConnectError):
    """A URL or host that must not be fetched.

    Subclasses ``httpx.ConnectError`` so the fetchers' existing
    ``except httpx.HTTPError`` handlers treat a refusal like any other
    connection failure, and so a refusal raised inside the transport
    surfaces through httpx unchanged.
    """


def _public_address(address: str) -> bool:
    """True when ``address`` is a globally routable unicast IP."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # ::ffff:127.0.0.1 and friends reach the IPv4 stack; judge the
        # wrapped address. 6to4/Teredo embed an IPv4 address too; refuse
        # them rather than reason about the tunnel endpoint.
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None or ip.teredo is not None:
            return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return False
    return ip.is_global


def resolve_public(host: str) -> list[str]:
    """Resolve ``host`` and return its addresses, all verified public.

    Raises ``UnsafeURL`` if the host is empty, does not resolve, or if any
    resolved address is not public. Rejecting on *any* bad address (rather
    than filtering) closes the trick of pairing one public address with one
    private one and letting the client pick.
    """
    host = (host or "").strip().strip("[]")
    if not host:
        raise UnsafeURL("blocked fetch: empty host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not _public_address(host):
            raise UnsafeURL(f"blocked fetch: {host} is not a public address")
        return [host]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeURL(f"blocked fetch: {host} does not resolve ({e})") from e
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise UnsafeURL(f"blocked fetch: {host} does not resolve")
    for address in addresses:
        if not _public_address(address):
            raise UnsafeURL(
                f"blocked fetch: {host} resolves to non-public address {address}"
            )
    return addresses


def check_url(url: str) -> list[str]:
    """Validate a URL for fetching; return the host's validated addresses.

    Raises ``UnsafeURL`` for a non-http(s) scheme, a disallowed port, or a
    host that fails ``resolve_public``.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as e:
        raise UnsafeURL(f"blocked fetch: unparsable URL ({e})") from e
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURL(f"blocked fetch: scheme {parsed.scheme!r} not allowed")
    if port is not None and port not in ALLOWED_PORTS:
        raise UnsafeURL(f"blocked fetch: port {port} not allowed")
    return resolve_public(host)


def url_is_safe(url: str) -> bool:
    """True when ``url`` passes ``check_url``."""
    try:
        check_url(url)
    except UnsafeURL:
        return False
    return True


def url_port(url: str) -> int:
    """Effective TCP port of an http(s) URL."""
    parsed = urlparse(url)
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


class SafeBackend(httpcore.SyncBackend):
    """httpcore network backend that connects only to validated addresses.

    ``connect_tcp`` receives the request's hostname; it resolves it through
    ``resolve_public`` and dials the validated IP addresses in order, so the
    socket can only reach an address that passed the check. The caller
    (``httpcore``'s connection) starts TLS on the returned stream with the
    original hostname, so SNI and certificate checks are unaffected.
    """

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.NetworkStream:
        addresses = resolve_public(host)
        if port not in ALLOWED_PORTS:
            raise UnsafeURL(f"blocked fetch: port {port} not allowed")
        last_error: Exception | None = None
        for address in addresses:
            try:
                return super().connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as e:
                last_error = e
        assert last_error is not None
        raise last_error


class SafeTransport(httpx.HTTPTransport):
    """``httpx.HTTPTransport`` whose connection pool uses ``SafeBackend``.

    Mirrors ``HTTPTransport``'s direct (non-proxy) pool construction. Proxies
    are deliberately unsupported: a proxy resolves the hostname itself, which
    would bypass the pinning.
    """

    def __init__(
        self,
        verify=True,
        cert=None,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: httpx.Limits = httpx._config.DEFAULT_LIMITS,
        local_address: str | None = None,
        retries: int = 0,
        socket_options=None,
        network_backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        super().__init__(
            verify=verify,
            cert=cert,
            trust_env=trust_env,
            http1=http1,
            http2=http2,
            limits=limits,
            local_address=local_address,
            retries=retries,
            socket_options=socket_options,
        )
        ssl_context = httpx.create_ssl_context(
            verify=verify, cert=cert, trust_env=trust_env
        )
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=http1,
            http2=http2,
            local_address=local_address,
            retries=retries,
            socket_options=socket_options,
            network_backend=network_backend or SafeBackend(),
        )


def safe_client(**kwargs) -> httpx.Client:
    """Return an ``httpx.Client`` on ``SafeTransport`` with redirects off.

    Redirects are followed only through ``safe_get`` so every hop is
    validated. ``proxy``/``mounts`` are refused because they would route
    around the pinned transport; passing ``transport=`` disables httpx's
    environment proxy pickup as well.
    """
    for key in ("proxy", "proxies", "mounts", "transport"):
        if key in kwargs:
            raise ValueError(f"safe_client does not accept {key!r}")
    kwargs.pop("follow_redirects", None)
    verify = kwargs.pop("verify", True)
    cert = kwargs.pop("cert", None)
    trust_env = kwargs.pop("trust_env", True)
    transport = SafeTransport(verify=verify, cert=cert, trust_env=trust_env)
    return httpx.Client(transport=transport, follow_redirects=False, **kwargs)


def safe_get(
    client: httpx.Client,
    url: str,
    *,
    max_redirects: int = MAX_REDIRECTS,
    **kwargs,
) -> httpx.Response:
    """GET ``url``, following redirects by hand and validating every hop.

    ``kwargs`` go to ``client.build_request``. Redirect requests are the ones
    httpx itself would have built (method changes, cross-origin header
    stripping), each checked with ``check_url`` before it is sent. The final
    response carries the redirect history like a normal followed request.
    """
    check_url(url)
    request = client.build_request("GET", url, **kwargs)
    history: list[httpx.Response] = []
    while True:
        response = client.send(request, follow_redirects=False)
        response.history = list(history)
        if not response.has_redirect_location or response.next_request is None:
            return response
        history.append(response)
        if len(history) > max_redirects:
            raise httpx.TooManyRedirects(
                "Exceeded maximum allowed redirects.", request=request
            )
        request = response.next_request
        check_url(str(request.url))
