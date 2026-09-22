"""Public-network-only HTTP transport and downloader egress proxy.

DNS is resolved once for each upstream connection. The socket connects to an
already checked numeric address; TLS still verifies the original hostname.
The loopback proxy is an egress control for configured downloader clients, not
an operating-system sandbox for arbitrary executable code.
"""
from __future__ import annotations

import base64
import hmac
import http.client
import io
import ipaddress
import secrets
import select
import socket
import socketserver
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from urllib.parse import urljoin, urlsplit, urlunsplit

from server.errors import IngestError

_REDIRECTS = {301, 302, 303, 307, 308}
_SAFE_HEADERS = {"accept", "accept-language", "user-agent", "range", "referer", "origin"}
_DEFAULT_UA = "VideoIngest/0.1"


def unsafe(message: str = "The destination is not permitted.") -> IngestError:
    return IngestError("UNSAFE_URL", message, next_action="use_supported_public_video")


def parse_network_url(url: str):
    if not isinstance(url, str) or not url or len(url) > 16384:
        raise unsafe("The URL is missing or exceeds the size limit.")
    if any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise unsafe("Whitespace, control characters and backslashes are forbidden in URLs.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise unsafe("The URL authority is malformed.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise unsafe("Only absolute HTTP and HTTPS URLs are permitted.")
    if parsed.username is not None or parsed.password is not None:
        raise unsafe("Credentials in URLs are forbidden.")
    if "%" in hostname or hostname.endswith(".") or not hostname.isascii():
        raise unsafe("The hostname is not permitted.")
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        raise unsafe("Nonstandard destination ports are forbidden.")
    if any(not label for label in hostname.split(".")):
        raise unsafe("The hostname is malformed.")
    return parsed


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    # Mapped and transition addresses can obscure an IPv4 destination.
    if isinstance(ip, ipaddress.IPv6Address) and (
        ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None
        or ip.is_site_local or ip in ipaddress.ip_network("64:ff9b::/96")
    ):
        return False
    return bool(ip.is_global and not (ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_private))


@dataclass(frozen=True)
class PublicAddress:
    family: int
    sockaddr: tuple


def resolve_public(host: str, port: int, resolver=None) -> list[PublicAddress]:
    """Reject a mixed public/private DNS answer instead of choosing the safe part."""
    resolver = resolver or socket.getaddrinfo
    try:
        records = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise IngestError("NETWORK_ERROR", "The destination could not be resolved.",
                          stage="resolving", retryable=True, next_action="retry_later") from exc
    result: list[PublicAddress] = []
    for family, socktype, proto, canonname, sockaddr in records:
        if family not in {socket.AF_INET, socket.AF_INET6} or not is_public_ip(sockaddr[0]):
            raise unsafe()
        address = PublicAddress(family, sockaddr)
        if address not in result:
            result.append(address)
    if not result:
        raise unsafe("The hostname did not resolve to a public address.")
    return result


def connect_pinned(addresses: list[PublicAddress], timeout: float) -> socket.socket:
    last_error: OSError | None = None
    deadline = time.monotonic() + timeout
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("The connection timeout was exceeded")
        sock = socket.socket(address.family, socket.SOCK_STREAM)
        sock.settimeout(remaining)
        try:
            # No hostname is passed here, so a second DNS answer cannot rebind it.
            sock.connect(address.sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError("No upstream address was available")


class _DeadlineReader(io.RawIOBase):
    """Check the total deadline on each socket receive, including header reads."""
    def __init__(self, sock, timeout, deadline, cancelled):
        super().__init__()
        self.sock, self.timeout, self.deadline, self.cancelled = sock, timeout, deadline, cancelled
        # SocketIO retains the descriptor when HTTPConnection closes on HTTP/1.0.
        self.raw = sock.makefile("rb", buffering=0)

    def close(self):
        self.raw.close()
        super().close()

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.cancelled():
            raise IngestError("CANCELLED", "The network read was cancelled.", stage="downloading", next_action="none")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP operation exceeded its total deadline")
        self.sock.settimeout(min(self.timeout, remaining))
        return self.raw.readinto(buffer)


class _DeadlineSocket:
    def __init__(self, sock, timeout, deadline, cancelled):
        self.sock, self.timeout, self.deadline, self.cancelled = sock, timeout, deadline, cancelled

    def makefile(self, mode, buffering=None):
        if mode != "rb":
            raise ValueError("The HTTP response requires a binary reader")
        return io.BufferedReader(_DeadlineReader(self.sock, self.timeout, self.deadline, self.cancelled))

    def __getattr__(self, name):
        return getattr(self.sock, name)


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, addresses: list[PublicAddress], *,
                 secure: bool, timeout: float, context: ssl.SSLContext, deadline=None, cancelled=None):
        super().__init__(host, port, timeout=timeout)
        self.addresses, self.secure, self.context = addresses, secure, context
        self.deadline, self.cancelled = deadline, cancelled or (lambda: False)

    def connect(self):
        sock = connect_pinned(self.addresses, self.timeout)
        try:
            if self.secure:
                sock = self.context.wrap_socket(sock, server_hostname=self.host)
            self.sock = _DeadlineSocket(sock, self.timeout, self.deadline, self.cancelled) if self.deadline else sock
        except BaseException:
            sock.close()
            raise


class SafeHTTPClient:
    def __init__(self, timeout: float = 20, max_redirects: int = 5, *, resolver=None,
                 cancelled: Callable[[], bool] | None = None, total_timeout: float = 60):
        if timeout <= 0 or total_timeout <= 0 or not 0 <= max_redirects <= 10:
            raise ValueError("Invalid network limits")
        self.timeout, self.max_redirects, self.resolver = timeout, max_redirects, resolver
        self.context = ssl.create_default_context()
        self.cancelled, self.total_timeout = cancelled or (lambda: False), total_timeout

    @staticmethod
    def _headers(headers: dict[str, str] | None) -> dict[str, str]:
        result = {"User-Agent": _DEFAULT_UA, "Accept-Encoding": "identity"}
        for name, value in (headers or {}).items():
            if name.lower() not in _SAFE_HEADERS:
                continue
            if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise unsafe("An HTTP header is malformed.")
            result[name] = value
        return result

    def _request(self, url: str, headers: dict[str, str] | None = None, deadline=None):
        if self.cancelled():
            raise IngestError("CANCELLED", "The network read was cancelled.", stage="downloading", next_action="none")
        parsed = parse_network_url(url)
        secure = parsed.scheme == "https"
        port = 443 if secure else 80
        addresses = resolve_public(parsed.hostname, port, self.resolver)
        remaining = self.timeout if deadline is None else min(self.timeout, deadline - time.monotonic())
        if remaining <= 0:
            raise IngestError("NETWORK_ERROR", "The HTTP operation timed out.", stage="downloading", retryable=True, next_action="retry_later")
        conn = _PinnedConnection(parsed.hostname, port, addresses, secure=secure,
                                 timeout=remaining, context=self.context, deadline=deadline, cancelled=self.cancelled)
        try:
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            conn.request("GET", path, headers=self._headers(headers))
            return conn, conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise IngestError("NETWORK_ERROR", "The public destination could not be read.",
                              stage="downloading", retryable=True, next_action="retry_later") from exc
        except BaseException:
            conn.close()
            raise

    def _open(self, url: str, headers=None):
        deadline = time.monotonic() + self.total_timeout
        visited = set()
        for step in range(self.max_redirects + 1):
            if url in visited:
                raise unsafe("The URL contains a redirect loop.")
            visited.add(url)
            conn, response = self._request(url, headers, deadline=deadline)
            if response.status in _REDIRECTS:
                location = response.getheader("Location")
                response.close()
                conn.close()
                if not location or step == self.max_redirects:
                    raise unsafe("The URL exceeds the redirect limit or has no redirect destination.")
                if len(location) > 16384:
                    raise unsafe("The redirect URL exceeds the size limit.")
                new_url = urljoin(url, location)
                parse_network_url(new_url)
                if urlsplit(url).scheme == "https" and urlsplit(new_url).scheme != "https":
                    raise unsafe("HTTPS downgrade redirects are forbidden.")
                if urlsplit(url).netloc != urlsplit(new_url).netloc:
                    headers = {k: v for k, v in (headers or {}).items()
                               if k.lower() not in {"referer", "origin"}}
                url = new_url
                continue
            if response.status >= 400:
                status = response.status
                response.close()
                conn.close()
                code = {401: "AUTH_REQUIRED", 403: "ACCESS_DENIED", 404: "CONTENT_UNAVAILABLE",
                        410: "CONTENT_UNAVAILABLE", 429: "RATE_LIMITED"}.get(status, "NETWORK_ERROR")
                raise IngestError(code, f"The upstream returned HTTP {status}.", stage="downloading",
                                  retryable=status == 429 or status >= 500, next_action="retry_later" if status == 429 or status >= 500 else "check_source_access")
            if not 200 <= response.status < 300:
                response.close()
                conn.close()
                raise IngestError("NETWORK_ERROR", "The upstream returned an unsupported HTTP response.", stage="downloading")
            return url, conn, response
        raise unsafe("The URL exceeds the redirect limit.")

    def get_bytes(self, url: str, max_bytes: int, headers: dict[str, str] | None = None) -> bytes:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        _final_url, conn, response = self._open(url, headers)
        try:
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    if int(length) > max_bytes or int(length) < 0:
                        raise IngestError("LIMIT_EXCEEDED", "The upstream response exceeds the byte limit.", stage="downloading")
                except ValueError as exc:
                    raise IngestError("NETWORK_ERROR", "The upstream response has an invalid length.", stage="downloading") from exc
            result = bytearray()
            while len(result) <= max_bytes:
                chunk = response.read1(min(65536, max_bytes + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
            if len(result) > max_bytes:
                raise IngestError("LIMIT_EXCEEDED", "The upstream response exceeds the byte limit.", stage="downloading")
            if length is not None and not response.chunked and len(result) != int(length):
                raise IngestError("NETWORK_ERROR", "The upstream response ended before its declared length.", stage="downloading", retryable=True, next_action="retry_later")
            return bytes(result)
        except (OSError, http.client.HTTPException) as exc:
            raise IngestError("NETWORK_ERROR", "The upstream response was interrupted.", stage="downloading", retryable=True, next_action="retry_later") from exc
        finally:
            response.close()
            conn.close()

    def resolve_redirect(self, url: str) -> str:
        final_url, conn, response = self._open(url)
        response.close()
        conn.close()
        return final_url


class _ProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = False


class GuardedProxy:
    """Authenticated local proxy; every upstream socket is pinned to public DNS.

    `on_bytes` accounts all bytes received from upstream, including failed
    transfers, headers and TLS overhead. Its exception stops forwarding and is
    made available through `raise_if_error`. HTTP proxy environment variables
    are irrelevant to this transport.
    """
    def __init__(self, *, on_bytes: Callable[[int], None] | None = None,
                 cancelled: Callable[[], bool] | None = None, timeout: float = 30,
                 total_timeout: float = 1800, resolver=None):
        self.on_bytes, self.cancelled = on_bytes, cancelled or (lambda: False)
        self.timeout, self.total_timeout, self.resolver = timeout, total_timeout, resolver
        self._token = secrets.token_urlsafe(32)
        self._authorization = "Basic " + base64.b64encode(f"ingest:{self._token}".encode()).decode()
        self._lock = threading.Lock()
        self._active: set[socket.socket] = set()
        self._stop = threading.Event()
        self._error: Exception | None = None
        self._slots = threading.BoundedSemaphore(8)
        self._server = None
        self.url = ""

    def _record_error(self, exc: Exception):
        with self._lock:
            if self._error is None:
                self._error = exc
        self._stop.set()

    def raise_if_error(self):
        with self._lock:
            error = self._error
        if error:
            raise error

    def _charge(self, count: int):
        if self.on_bytes:
            with self._lock:
                self.on_bytes(count)

    def _relay(self, client: socket.socket, upstream: socket.socket):
        started = last_activity = time.monotonic()
        while not self._stop.is_set():
            if self.cancelled():
                raise IngestError("CANCELLED", "The network transfer was cancelled.", stage="downloading", next_action="none")
            now = time.monotonic()
            if now - started > self.total_timeout or now - last_activity > self.timeout:
                raise IngestError("NETWORK_ERROR", "The network transfer timed out.", stage="downloading", retryable=True, next_action="retry_later")
            readable, _, _ = select.select([client, upstream], [], [], 0.25)
            for source in readable:
                chunk = source.recv(65536)
                if not chunk:
                    return
                last_activity = time.monotonic()
                if source is upstream:
                    self._charge(len(chunk))
                    client.sendall(chunk)
                else:
                    upstream.sendall(chunk)

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # Avoid buffered reads consuming TLS bytes before CONNECT relay.
            rbufsize = 0

            def log_message(self, format, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(owner.timeout)

            def _handle(self, tunnel: bool = False):
                upstream = None
                acquired = False
                try:
                    provided = self.headers.get("Proxy-Authorization", "")
                    if not hmac.compare_digest(provided, owner._authorization):
                        self.send_error(407, "Proxy authentication required")
                        return
                    if owner._stop.is_set() or not owner._slots.acquire(blocking=False):
                        self.send_error(503, "Proxy unavailable")
                        return
                    acquired = True
                    if tunnel:
                        parsed = parse_network_url("https://" + self.path)
                        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                            raise unsafe("The CONNECT authority is malformed.")
                    else:
                        parsed = parse_network_url(self.path)
                        if parsed.scheme != "http":
                            raise unsafe("HTTPS requires a verified CONNECT tunnel.")
                    port = 443 if tunnel else 80
                    addresses = resolve_public(parsed.hostname, port, owner.resolver)
                    upstream = connect_pinned(addresses, owner.timeout)
                    with owner._lock:
                        owner._active.update({self.connection, upstream})
                    if tunnel:
                        self.send_response(200, "Connection established")
                        self.end_headers()
                    else:
                        if self.headers.get("Transfer-Encoding"):
                            raise unsafe("Chunked upload requests are forbidden.")
                        try:
                            content_length = int(self.headers.get("Content-Length", "0"))
                        except ValueError as exc:
                            raise unsafe("The upload length is malformed.") from exc
                        if not 0 <= content_length <= 2 * 1024**2:
                            raise unsafe("The upload exceeds the byte limit.")
                        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                        outgoing = [f"{self.command} {path} HTTP/1.1", f"Host: {parsed.netloc}", "Connection: close"]
                        connection_tokens = {v.strip().lower() for v in self.headers.get("Connection", "").split(",")}
                        excluded = {"proxy-authorization", "proxy-connection", "connection", "host", "keep-alive", "upgrade", "te", "trailer", "transfer-encoding", "authorization", "cookie"} | connection_tokens
                        for name, value in self.headers.items():
                            if name.lower() not in excluded:
                                if any(ord(c) < 32 or ord(c) == 127 for c in value):
                                    raise unsafe("An upstream header is malformed.")
                                outgoing.append(f"{name}: {value}")
                        upstream.sendall(("\r\n".join(outgoing) + "\r\n\r\n").encode("latin-1"))
                        remaining = content_length
                        while remaining:
                            chunk = self.rfile.read(min(65536, remaining))
                            if not chunk:
                                raise unsafe("The upload was interrupted.")
                            remaining -= len(chunk)
                            upstream.sendall(chunk)
                    owner._relay(self.connection, upstream)
                except IngestError as exc:
                    owner._record_error(exc)
                    try:
                        self.send_error(403, "Network policy rejected the request")
                    except OSError:
                        pass
                except (OSError, ValueError, http.client.HTTPException):
                    # Ordinary connection failures remain downloader-retryable.
                    try:
                        self.send_error(502, "Upstream connection failed")
                    except OSError:
                        pass
                except Exception as exc:  # noqa: BLE001 - callback failures must stop and reach the parent.
                    owner._record_error(exc)
                finally:
                    self.close_connection = True
                    if upstream:
                        with owner._lock:
                            owner._active.discard(upstream)
                            owner._active.discard(self.connection)
                        upstream.close()
                    if acquired:
                        owner._slots.release()

            def do_CONNECT(self):
                self._handle(True)

            def do_GET(self):
                self._handle()

            do_HEAD = do_GET
            do_POST = do_GET

        self._server = _ProxyServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        self.url = f"http://ingest:{self._token}@127.0.0.1:{self._server.server_address[1]}"
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop.set()
        if self._server:
            self._server.shutdown()
            with self._lock:
                active = list(self._active)
            for sock in active:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._server.server_close()
            self._thread.join(timeout=2)
