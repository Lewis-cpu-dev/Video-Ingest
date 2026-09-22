import base64
import contextlib
import http.client
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import pytest

from server.errors import IngestError
from server.security import network
from server.security.network import GuardedProxy, PublicAddress, SafeHTTPClient, is_public_ip, resolve_public

PUBLIC = "93.184.216.34"


def answer(ip, port=443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "0.0.0.0", "10.0.0.1", "172.16.2.3", "192.168.0.1", "169.254.169.254",
    "100.100.100.200", "100.64.0.1", "192.0.2.1", "198.51.100.1", "203.0.113.5", "224.0.0.1",
    "255.255.255.255", "::1", "::", "fc00::1", "fe80::1", "ff02::1", "2001:db8::1",
    "fec0::1", "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "::ffff:127.0.0.1", "::ffff:93.184.216.34", "2002:7f00:0001::1", "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
])
def test_nonpublic_and_transition_ip_denied(ip):
    assert not is_public_ip(ip)
    with pytest.raises(IngestError) as err:
        resolve_public("cdn.example", 443, lambda *a, **kw: answer(ip))
    assert err.value.code == "UNSAFE_URL"


@pytest.mark.parametrize("ip", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111"])
def test_public_ip_allowed(ip):
    assert is_public_ip(ip)
    assert resolve_public("cdn.example", 443, lambda *a, **kw: answer(ip))[0].sockaddr[0] == ip


def test_mixed_dns_answers_fail_closed():
    with pytest.raises(IngestError):
        resolve_public("cdn.example", 443, lambda *a, **kw: answer(PUBLIC) + answer("127.0.0.1"))


def test_connect_uses_pinned_numeric_address_without_dns(monkeypatch):
    connected = []
    class FakeSocket:
        def settimeout(self, timeout):
            pass
        def connect(self, sockaddr):
            connected.append(sockaddr)
        def close(self):
            pass
    def fail_dns(*args, **kwargs):
        pytest.fail("The pinned connection must not resolve DNS a second time")
    monkeypatch.setattr(network.socket, "socket", lambda *a, **kw: FakeSocket())
    monkeypatch.setattr(network.socket, "getaddrinfo", fail_dns)
    network.connect_pinned([PublicAddress(socket.AF_INET, (PUBLIC, 443))], 2)
    assert connected == [(PUBLIC, 443)]


def test_tls_verifies_original_hostname(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(network, "connect_pinned", lambda *a: sentinel)
    calls = []
    class Context:
        def wrap_socket(self, sock, server_hostname):
            calls.append((sock, server_hostname))
            return sentinel
    conn = network._PinnedConnection("cdn.example", 443, [], secure=True, timeout=2, context=Context())
    conn.connect()
    assert calls == [(sentinel, "cdn.example")]
    context = SafeHTTPClient().context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


@contextlib.contextmanager
def upstream_server():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://internal.example/secret")
                self.end_headers()
            elif self.path == "/redirect-public":
                self.send_response(302)
                self.send_header("Location", "http://second.example/result")
                self.end_headers()
            elif self.path == "/loop":
                self.send_response(302)
                self.send_header("Location", "/loop")
                self.end_headers()
            elif self.path == "/no-length":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"x" * 100)
            else:
                self.send_response(200)
                self.send_header("Content-Length", "12")
                self.end_headers()
                self.wfile.write(b"real payload")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield server, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


@pytest.fixture
def public_to_fixture(monkeypatch):
    with upstream_server() as (server, requests):
        connections = []
        def connect(addresses, timeout):
            # Fixture maps a validated public IP to a local test server only here.
            assert all(a.sockaddr[0] == PUBLIC for a in addresses)
            connections.append(addresses)
            return socket.create_connection(server.server_address, timeout)
        monkeypatch.setattr(network, "connect_pinned", connect)
        yield requests, connections


def test_get_bytes_ignores_proxy_environment_and_filters_headers(public_to_fixture, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    client = SafeHTTPClient(resolver=lambda *a, **kw: answer(PUBLIC, 80))
    assert client.get_bytes("http://public.example/data", 20, headers={"Authorization": "secret", "Cookie": "secret", "Proxy-Authorization": "secret", "Host": "private", "Accept": "text/plain"}) == b"real payload"
    request = public_to_fixture[0][0]
    assert request[1]["Host"] == "public.example"
    assert request[1]["Accept"] == "text/plain"
    assert not {"Authorization", "Cookie", "Proxy-Authorization"} & request[1].keys()


def test_redirect_to_private_is_blocked_before_connect(public_to_fixture):
    calls = []
    def resolver(host, port, **kwargs):
        calls.append(host)
        return answer("127.0.0.1" if host == "internal.example" else PUBLIC, port)
    client = SafeHTTPClient(resolver=resolver)
    with pytest.raises(IngestError) as err:
        client.get_bytes("http://public.example/redirect", 20)
    assert err.value.code == "UNSAFE_URL"
    assert calls == ["public.example", "internal.example"]
    assert len(public_to_fixture[1]) == 1
    assert [req[0] for req in public_to_fixture[0]] == ["/redirect"]


def test_every_public_redirect_is_resolved_and_origin_headers_removed(public_to_fixture):
    calls = []
    def resolver(host, port, **kwargs):
        calls.append(host)
        return answer(PUBLIC, port)
    client = SafeHTTPClient(resolver=resolver)
    assert client.get_bytes("http://public.example/redirect-public", 20, {"Referer": "http://private-context", "Origin": "http://private-context"}) == b"real payload"
    assert calls == ["public.example", "second.example"]
    assert "Referer" not in public_to_fixture[0][1][1]
    assert "Origin" not in public_to_fixture[0][1][1]


def test_dns_rebinding_on_later_request_is_rejected(public_to_fixture):
    calls = 0
    def resolver(host, port, **kwargs):
        nonlocal calls
        calls += 1
        return answer(PUBLIC if calls == 1 else "127.0.0.1", port)
    client = SafeHTTPClient(resolver=resolver)
    assert client.get_bytes("http://public.example/data", 20) == b"real payload"
    with pytest.raises(IngestError):
        client.get_bytes("http://public.example/data", 20)
    assert len(public_to_fixture[1]) == 1


@pytest.mark.parametrize("path", ["/data", "/no-length"])
def test_response_size_limit_with_and_without_content_length(public_to_fixture, path):
    client = SafeHTTPClient(resolver=lambda *a, **kw: answer(PUBLIC, 80))
    with pytest.raises(IngestError) as err:
        client.get_bytes("http://public.example" + path, 8)
    assert err.value.code == "LIMIT_EXCEEDED"


def test_redirect_loop_is_bounded(public_to_fixture):
    client = SafeHTTPClient(resolver=lambda *a, **kw: answer(PUBLIC, 80))
    with pytest.raises(IngestError):
        client.resolve_redirect("http://public.example/loop")
    assert len(public_to_fixture[1]) == 1


def proxy_request(proxy, url, method="GET", authenticated=True):
    parts = urlsplit(proxy.url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=3)
    credentials = base64.b64encode(f"{parts.username}:{parts.password}".encode()).decode()
    headers = {"Proxy-Authorization": f"Basic {credentials}"} if authenticated else {}
    conn.request(method, url, headers=headers)
    response = conn.getresponse()
    body = response.read()
    status = response.status
    conn.close()
    return status, body


def test_proxy_forwards_pinned_http_and_charges_raw_bytes(public_to_fixture):
    charges = []
    with GuardedProxy(resolver=lambda *a, **kw: answer(PUBLIC, 80), on_bytes=charges.append) as proxy:
        status, body = proxy_request(proxy, "http://public.example/data")
        assert status == 200 and body == b"real payload"
        proxy.raise_if_error()
    assert sum(charges) >= len(body)
    assert len(public_to_fixture[1]) == 1


def test_proxy_requires_ephemeral_authentication(public_to_fixture):
    with GuardedProxy(resolver=lambda *a, **kw: answer(PUBLIC, 80)) as proxy:
        status, _body = proxy_request(proxy, "http://public.example/data", authenticated=False)
        assert status == 407
    assert not public_to_fixture[1]


@pytest.mark.parametrize("target,method", [("http://private.example/data", "GET"), ("private.example:443", "CONNECT")])
def test_proxy_blocks_private_http_and_connect(target, method):
    with GuardedProxy(resolver=lambda *a, **kw: answer("169.254.169.254")) as proxy:
        status, _body = proxy_request(proxy, target, method)
        assert status == 403
        with pytest.raises(IngestError) as err:
            proxy.raise_if_error()
        assert err.value.code == "UNSAFE_URL"


def test_proxy_budget_failure_stops_transfer_and_preserves_error(public_to_fixture):
    def budget(delta):
        raise IngestError("BUDGET_EXCEEDED", "Download budget exhausted")
    with GuardedProxy(resolver=lambda *a, **kw: answer(PUBLIC, 80), on_bytes=budget) as proxy:
        status, _body = proxy_request(proxy, "http://public.example/data")
        assert status == 403
        with pytest.raises(IngestError) as err:
            proxy.raise_if_error()
        assert err.value.code == "BUDGET_EXCEEDED"


def test_cancelled_http_request_never_reaches_upstream(public_to_fixture):
    client = SafeHTTPClient(cancelled=lambda: True, resolver=lambda *a, **kw: answer(PUBLIC, 80))
    with pytest.raises(IngestError) as err:
        client.get_bytes("http://public.example/data", 20)
    assert err.value.code == "CANCELLED"
    assert not public_to_fixture[1]


def test_deadline_applies_to_socket_reads(monkeypatch):
    closed = []
    class FakeRaw:
        def readinto(self, data):
            pytest.fail("Expired read must never receive from socket")
        def close(self):
            closed.append(True)
    class FakeSocket:
        def makefile(self, *args, **kwargs):
            return FakeRaw()
    reader = network._DeadlineReader(FakeSocket(), 2, 0, lambda: False)
    with pytest.raises(TimeoutError):
        reader.readinto(bytearray(1))
    reader.close()
    assert closed


def test_proxy_public_connect_pins_upstream_and_relays_bytes(monkeypatch):
    # A socket pair represents a public upstream after its IP was validated.
    upstream, peer = socket.socketpair()
    destinations = []
    def connect(addresses, timeout):
        destinations.extend(addresses)
        return upstream
    monkeypatch.setattr(network, "connect_pinned", connect)
    charges = []
    with GuardedProxy(resolver=lambda *a, **kw: answer(PUBLIC), on_bytes=charges.append) as proxy:
        parsed = urlsplit(proxy.url)
        sock = socket.create_connection((parsed.hostname, parsed.port), timeout=3)
        try:
            token = base64.b64encode(f"{parsed.username}:{parsed.password}".encode()).decode()
            sock.sendall(f"CONNECT cdn.example:443 HTTP/1.1\r\nHost: cdn.example:443\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode())
            response = bytearray()
            while b"\r\n\r\n" not in response:
                response.extend(sock.recv(1))
            assert response.startswith(b"HTTP/1.1 200")
            sock.sendall(b"client bytes")
            peer.settimeout(3)
            assert peer.recv(100) == b"client bytes"
            peer.sendall(b"upstream bytes")
            assert sock.recv(100) == b"upstream bytes"
            proxy.raise_if_error()
        finally:
            sock.close()
            peer.close()
    assert destinations[0].sockaddr == (PUBLIC, 443)
    assert sum(charges) == len(b"upstream bytes")


def test_proxy_revalidates_real_http_client_redirect(public_to_fixture, monkeypatch):
    monkeypatch.setenv("no_proxy", "")
    def resolver(host, port, **kwargs):
        return answer("127.0.0.1" if host == "internal.example" else PUBLIC, port)
    with GuardedProxy(resolver=resolver) as proxy:
        opener = build_opener(ProxyHandler({"http": proxy.url, "https": proxy.url}))
        with pytest.raises(HTTPError):
            opener.open("http://public.example/redirect", timeout=3)
        with pytest.raises(IngestError) as err:
            proxy.raise_if_error()
        assert err.value.code == "UNSAFE_URL"
    assert len(public_to_fixture[1]) == 1


def test_downloader_child_rejects_direct_sockets_dns_and_commands():
    script = """
import socket, subprocess, sys
from server.errors import IngestError
from server.providers.ytdlp_worker import _network_lock
_network_lock('http://ingest:token@127.0.0.1:32123')
def blocked(action):
    try:
        action()
    except IngestError:
        return
    raise AssertionError('Direct egress or execution was allowed')
blocked(lambda: socket.create_connection(('127.0.0.1', 9)))
blocked(lambda: socket.socket().connect_ex(('93.184.216.34', 443)))
blocked(lambda: socket.getaddrinfo('public.example', 443))
blocked(lambda: subprocess.Popen([sys.executable, '-c', 'pass']))
print('all blocked')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "all blocked"


@pytest.mark.parametrize("unsafe_redirect", [False, True])
def test_real_ytdlp_urllib_handler_uses_guarded_proxy(public_to_fixture, unsafe_redirect):
    script = """
import sys
from yt_dlp.networking import Request
from yt_dlp.networking._urllib import UrllibRH
from server.providers.ytdlp_worker import SilentLogger, _network_lock
_network_lock(sys.argv[1])
with UrllibRH(logger=SilentLogger(), proxies={'http': sys.argv[1], 'https': sys.argv[1]}) as handler:
    response = handler.send(Request(sys.argv[2]))
    print(response.read().decode())
"""
    charges = []
    def resolver(host, port, **kwargs):
        return answer("127.0.0.1" if host == "internal.example" else PUBLIC, port)
    with GuardedProxy(resolver=resolver, on_bytes=charges.append) as proxy:
        url = "http://public.example/redirect" if unsafe_redirect else "http://public.example/data"
        result = subprocess.run([sys.executable, "-c", script, proxy.url, url], capture_output=True, text=True,
                                timeout=10, check=False)
        if unsafe_redirect:
            with pytest.raises(IngestError) as err:
                proxy.raise_if_error()
            assert err.value.code == "UNSAFE_URL"
        else:
            proxy.raise_if_error()
    if unsafe_redirect:
        assert result.returncode != 0
        assert not result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "real payload"
    assert sum(charges) >= len(b"real payload")
    assert len(public_to_fixture[1]) == 1
