"""Disposable loopback adapter tests; no production ports, state or credentials."""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
from pathlib import Path
import socket
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("projector_proxy_under_test", ROOT / "ops/projector_proxy.py")
proxy_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy_module)
HOST = "scoreboard-example.gobrev.dev"
ORIGIN = "https://" + HOST


@pytest.fixture
def proxy(monkeypatch):
    class State:
        requests = []
        reply = None

    state = State()

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers)})
            if state.reply:
                state.reply(self)
                return
            body = b'{"participants": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", proxy_module.DEFAULT_CSP)
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Set-Cookie", "synthetic-cookie=not-for-viewer")
            self.send_header("Authorization", "synthetic-not-for-viewer")
            self.send_header("X-Private-Header", "synthetic-not-for-viewer")
            self.send_header("Location", "https://untrusted.invalid/")
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    upstream.handle_error = lambda *_args: None
    monkeypatch.setattr(proxy_module, "LISTEN_PORT", 0)
    monkeypatch.setattr(proxy_module, "UPSTREAM_PORT", upstream.server_port)
    monkeypatch.setattr(proxy_module, "SOCKET_TIMEOUT", 1)
    monkeypatch.setattr(proxy_module, "REQUEST_DEADLINE", 3)
    monkeypatch.setattr(proxy_module, "MAX_WORKERS", 2)
    server = proxy_module.ProjectorServer(HOST)
    threads = [threading.Thread(target=lambda service=service: service.serve_forever(poll_interval=.01), daemon=True)
               for service in (upstream, server)]
    for thread in threads:
        thread.start()
    try:
        yield server, upstream, state
    finally:
        for service in (server, upstream):
            service.shutdown()
            service.server_close()
        for thread in threads:
            thread.join(timeout=5)


def request(server, path="/display", method="GET", headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request(method, path, headers={"Host": HOST, **(headers or {})})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def raw(server, request_bytes):
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=3) as connection:
        connection.sendall(request_bytes)
        result = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                return result
            result += chunk
            assert len(result) <= 3 * 1024 * 1024


def status(raw_response):
    return int(raw_response.split(b"\r\n", 1)[0].split()[1])


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.01)


@pytest.mark.parametrize("hostname", [HOST, "scoreboard.example.com", "a.b-c.test"])
def test_exact_dns_hostname_configuration(hostname):
    assert proxy_module.validated_hostname(hostname) == hostname


@pytest.mark.parametrize("address", ["127.0.0.1", "100.64.0.0", "100.127.255.255", "100.64.0.10"])
def test_bind_and_peer_addresses_are_exact_private_ipv4(address):
    assert proxy_module.validated_address(address) == address


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.2", "8.8.8.8", "192.168.1.2", "100.63.255.255",
                                     "100.128.0.0", "100.64.0.0/10", "100.64.0.10:8093", "::1", "::ffff:127.0.0.1",
                                     "localhost", "", " 100.64.0.10", "100.064.0.10"])
def test_wildcard_public_subnet_or_ambiguous_addresses_are_rejected(address):
    with pytest.raises(ValueError):
        proxy_module.validated_address(address)


@pytest.mark.parametrize("peers", ["", [], "127.0.0.1,", "127.0.0.1,127.0.0.1", "127.0.0.1,8.8.8.8",
                                   "100.64.0.0/10", "100.64.0.21, 100.64.0.22"])
def test_peer_allowlist_is_nonempty_exact_and_unambiguous(peers):
    with pytest.raises(ValueError):
        proxy_module.validated_peers(peers)


def test_configured_gateway_list_is_exact_not_a_subnet():
    peers = proxy_module.validated_peers("100.64.0.21,100.64.0.22,100.64.0.23,100.64.0.10")
    assert peers == {"100.64.0.21", "100.64.0.22", "100.64.0.23", "100.64.0.10"}
    assert "100.64.0.24" not in peers


def test_unlisted_socket_peer_is_denied_before_http_or_worker_allocation(proxy):
    server, _, state = proxy
    server.allowed_peers = frozenset({"100.64.0.21"})
    code, _, _ = request(server, headers={"Forwarded": "for=100.64.0.21", "X-Forwarded-For": "100.64.0.21"})
    assert code == 403
    assert server.slots._value == 2
    assert state.requests == []


def test_listed_socket_peer_is_admitted_with_the_same_http_checks(proxy):
    server, _, state = proxy
    server.allowed_peers = proxy_module.validated_peers("127.0.0.1,100.64.0.21")
    assert request(server)[0] == 200
    assert request(server, headers={"Origin": "https://evil.invalid"})[0] == 403
    assert len(state.requests) == 1


@pytest.mark.parametrize("hostname", [
    "", "localhost", "127.0.0.1", "[::1]", "0.0.0.0", "https://" + HOST,
    HOST + ":443", HOST + "/display", HOST + ".", HOST.upper(), "*.gobrev.dev",
    "name.localhost", "name.local", "a..example.com", "-a.example.com", "a-.example.com",
    "a" * 64 + ".example.com", "한글.example.com", HOST + "\nOrigin: https://evil.invalid",
])
def test_invalid_hostname_configuration_is_rejected(hostname):
    with pytest.raises(ValueError):
        proxy_module.validated_hostname(hostname)


@pytest.mark.parametrize("route", sorted(proxy_module.ROUTES))
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_allowed_routes_and_head_get_semantics(proxy, route, method):
    server, upstream, state = proxy
    code, headers, body = request(server, route, method=method, headers={"Origin": ORIGIN})
    assert server.server_address[0] == "127.0.0.1"
    assert code == 200
    assert state.requests[-1]["method"] == "GET"
    assert state.requests[-1]["path"] == route
    assert state.requests[-1]["headers"]["Host"] == f"127.0.0.1:{upstream.server_port}"
    assert body == (b"" if method == "HEAD" else b'{"participants": []}')
    assert headers["Content-Length"] == str(len(b'{"participants": []}'))
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Content-Security-Policy"] == proxy_module.DEFAULT_CSP
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Connection"] == "close"
    assert not {"Set-Cookie", "Authorization", "X-Private-Header", "Location", "Server"}.intersection(headers)


def test_credentials_and_forwarded_headers_never_reach_upstream(proxy):
    server, _, state = proxy
    code, _, _ = request(server, "/api/board?challenge=2", headers={
        "Authorization": "Bearer SYNTHETIC_NOT_A_REAL_TOKEN",
        "Cookie": "SYNTHETIC_NOT_A_REAL_SESSION=1", "Origin": ORIGIN,
        "Forwarded": "for=untrusted;host=evil.invalid;proto=http",
        "X-Forwarded-Host": "evil.invalid", "X-Forwarded-Proto": "http",
        "X-Forwarded-For": "203.0.113.10", "X-HTTP-Method-Override": "POST",
    })
    assert code == 200
    assert state.requests[-1]["path"] == "/api/board?challenge=2"
    assert set(state.requests[-1]["headers"]) == {"Host", "Accept", "Accept-Encoding", "Connection"}
    assert state.requests[-1]["headers"]["Accept-Encoding"] == "identity"


@pytest.mark.parametrize("headers", [
    {"Host": "localhost:8093"}, {"Host": "127.0.0.1:8093"}, {"Host": HOST + ":443"},
    {"Host": "evil.invalid", "X-Forwarded-Host": HOST},
    {"Origin": "http://" + HOST}, {"Origin": "https://evil.invalid"},
    {"Origin": "null"}, {"Origin": ORIGIN + "/"},
])
def test_foreign_host_or_origin_is_rejected(proxy, headers):
    server, _, state = proxy
    assert request(server, headers=headers)[0] == 403
    assert state.requests == []


@pytest.mark.parametrize("lines", [
    ["Host: " + HOST, "Host: " + HOST],
    ["Host: " + HOST, "host: evil.invalid"],
    ["Host: " + HOST, "Origin: " + ORIGIN, "origin: " + ORIGIN],
    [],
])
def test_duplicate_or_missing_authority_headers_are_rejected(proxy, lines):
    server, _, state = proxy
    payload = ("GET /display HTTP/1.1\r\n" + "\r\n".join(lines) + "\r\n\r\n").encode()
    assert status(raw(server, payload)) == 403
    assert state.requests == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT", "BREW"])
def test_only_read_methods_are_allowed(proxy, method):
    server, _, state = proxy
    code, headers, _ = request(server, "/api/submissions", method=method)
    assert code == 405
    assert headers["Allow"] == "GET, HEAD"
    assert state.requests == []


@pytest.mark.parametrize("path", [
    "/api/me", "/api/me/nickname", "/api/submissions", "/api/reset", "/api/status",
    "/api/backups/example", "/admin", "/demo", "/health", "/favicon.ico",
    "/../api/board", "/%2e%2e/api/board", "/display%2f", "/api/board/",
])
def test_unknown_or_sensitive_paths_are_rejected(proxy, path):
    server, _, state = proxy
    assert request(server, path)[0] == 404
    assert state.requests == []


@pytest.mark.parametrize("target", [
    "http://127.0.0.1:8092/api/status", "http://169.254.169.254/", "//display",
    "//127.0.0.1:8092/api/status", "/display#fragment", "/display\\anything", "*",
])
def test_absolute_network_or_ambiguous_target_is_rejected(proxy, target):
    server, _, state = proxy
    assert status(raw(server, f"GET {target} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())) == 400
    assert state.requests == []


@pytest.mark.parametrize("framing", [
    "Content-Length: 1", "Content-Length: -1", "Content-Length: 0\r\nContent-Length: 0",
    "Transfer-Encoding: chunked", "Content-Length: 0\r\nTransfer-Encoding: chunked",
    "Expect: something", "Expect: 100-continue",
])
def test_request_bodies_and_ambiguous_framing_are_rejected(proxy, framing):
    server, _, state = proxy
    result = raw(server, f"GET /display HTTP/1.1\r\nHost: {HOST}\r\n{framing}\r\n\r\nX".encode())
    assert status(result) in {400, 417}
    assert state.requests == []


def test_close_after_one_request_prevents_pipelining(proxy):
    server, _, state = proxy
    get = f"GET /display HTTP/1.1\r\nHost: {HOST}\r\nConnection: keep-alive\r\n\r\n".encode()
    result = raw(server, get + get)
    assert result.count(b"HTTP/1.1 200") == 1
    assert len(state.requests) == 1


def test_upstream_query_errors_are_forwarded_without_rewriting(proxy):
    server, _, state = proxy

    def reply(handler):
        handler.send_response(400)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"{}")

    state.reply = reply
    code, _, body = request(server, "/api/board?challenge=bad&challenge=2")
    assert code == 400 and body == b"{}"
    assert state.requests[-1]["path"] == "/api/board?challenge=bad&challenge=2"


@pytest.mark.parametrize("kind", ["redirect", "oversized-declared", "oversized-stream", "wrong-type", "folded-type",
                                  "duplicate-length", "length-and-chunks", "bad-transfer", "compressed", "truncated"])
def test_invalid_upstream_responses_fail_closed(proxy, monkeypatch, kind):
    server, _, state = proxy
    monkeypatch.setattr(proxy_module, "MAX_BODY", 64)

    def reply(handler):
        handler.send_response(302 if kind == "redirect" else 200)
        mime = "application/octet-stream" if kind == "wrong-type" else "application/json\r\n folded" if kind == "folded-type" else "application/json"
        handler.send_header("Content-Type", mime)
        if kind in {"oversized-declared", "truncated"}:
            handler.send_header("Content-Length", "65" if kind == "oversized-declared" else "4")
        if kind in {"duplicate-length", "length-and-chunks"}:
            handler.send_header("Content-Length", "2")
            handler.send_header("Content-Length" if kind == "duplicate-length" else "Transfer-Encoding", "2" if kind == "duplicate-length" else "chunked")
        if kind == "bad-transfer":
            handler.send_header("Transfer-Encoding", "gzip")
        if kind == "compressed":
            handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Location", "http://127.0.0.1:8092/api/status")
        handler.end_headers()
        handler.wfile.write(b"X" * 65 if kind == "oversized-stream" else b"{}")

    state.reply = reply
    code, headers, _ = request(server)
    assert code == 502
    assert "Location" not in headers
    assert len(state.requests) == 1


def test_chunked_upstream_is_decoded_and_safely_reframed(proxy):
    server, _, state = proxy

    def reply(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        handler.wfile.write(b"2\r\n{}\r\n0\r\n\r\n")

    state.reply = reply
    code, headers, body = request(server)
    assert code == 200 and body == b"{}"
    assert headers["Content-Length"] == "2" and "Transfer-Encoding" not in headers


def test_request_size_limits(proxy):
    server, _, state = proxy
    assert request(server, "/display?" + "x" * proxy_module.MAX_TARGET)[0] == 414
    assert request(server, headers={"X-Oversized": "x" * proxy_module.MAX_HEADERS})[0] == 431
    assert state.requests == []


def test_header_bytes_are_bounded_before_parser_buffers_them():
    incoming = io.BytesIO(b"X" * (proxy_module.MAX_HEADERS * 4))
    reader = proxy_module.HeaderReader(incoming)
    with pytest.raises(proxy_module.HTTPException):
        reader.readline(65537)
    assert incoming.tell() == proxy_module.MAX_HEADERS + 1


def test_unavailable_upstream_is_bad_gateway(proxy, monkeypatch):
    server, _, _ = proxy
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        monkeypatch.setattr(proxy_module, "UPSTREAM_PORT", unused.getsockname()[1])
        assert request(server)[0] == 502


def test_inactive_upstream_times_out(proxy, monkeypatch):
    server, _, state = proxy
    monkeypatch.setattr(proxy_module, "SOCKET_TIMEOUT", .1)

    def reply(_handler):
        time.sleep(.3)

    state.reply = reply
    assert request(server)[0] == 504


def test_worker_limit_rejects_excess_connections_and_recovers(proxy):
    server, _, state = proxy
    connections = [socket.create_connection(server.server_address, timeout=2) for _ in range(2)]
    try:
        for connection in connections:
            connection.sendall(b"GET /display HTTP/1.1\r\n")
        eventually(lambda: server.slots._value == 0)
        assert request(server)[0] == 503
        assert state.requests == []
    finally:
        for connection in connections:
            connection.close()
    eventually(lambda: server.slots._value == 2)
    assert request(server)[0] == 200


def test_absolute_deadline_stops_slow_upstream_and_releases_worker(proxy, monkeypatch):
    server, _, state = proxy
    monkeypatch.setattr(proxy_module, "REQUEST_DEADLINE", .2)

    def reply(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        try:
            for _ in range(100):
                handler.wfile.write(b" ")
                time.sleep(.03)
        except OSError:
            pass

    state.reply = reply
    started = time.monotonic()
    with socket.create_connection(server.server_address, timeout=2) as connection:
        connection.sendall(f"GET /display HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
        assert connection.recv(1024) == b""
    eventually(lambda: server.slots._value == 2)
    assert time.monotonic() - started < 1


def test_absolute_deadline_stops_slow_headers(proxy, monkeypatch):
    server, _, state = proxy
    monkeypatch.setattr(proxy_module, "REQUEST_DEADLINE", .2)
    with socket.create_connection(server.server_address, timeout=2) as connection:
        connection.sendall(b"GET /display HTTP/1.1\r\nHost: ")
        eventually(lambda: server.slots._value == 1)
        eventually(lambda: server.slots._value == 2)
        assert connection.recv(1024) == b""
    assert state.requests == []


def test_service_limits_and_private_data_isolation():
    unit = (ROOT / "ops/ai4sci-projector-link.service").read_text()
    for directive in ("DynamicUser=yes", "NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict",
                      "ProtectHome=yes", "IPAddressDeny=any", "IPAddressAllow=localhost", "TasksMax=64",
                      "MemoryMax=256M", "RestrictAddressFamilies=AF_INET", "LimitNOFILE=128"):
        assert directive in unit
    assert "PrivateNetwork=yes" not in unit
    assert "ReadWritePaths=" not in unit
    assert "/usr/bin/python3 -I -B /usr/local/lib/ai4sci-projector-link/projector_proxy.py" in unit
    assert "--hostname ${PROJECTOR_HOSTNAME}" in unit
    assert "--bind-address ${PROJECTOR_BIND_ADDRESS}" in unit
    assert "--allowed-peers ${PROJECTOR_ALLOWED_PEERS}" in unit
    assert "Environment=PROJECTOR_BIND_ADDRESS=127.0.0.1" in unit
    assert "Environment=PROJECTOR_ALLOWED_PEERS=127.0.0.1" in unit
    assert "IPAddressAllow=100.64.0.0/10" not in unit
    for path in ("/srv/ai4sci-judge", "/var/lib/ai4sci-judge", "/var/lib/ai4sci-judge-admin",
                 "/var/lib/ai4sci-enrollment", "/etc/ai4sci-judge-admin", "/var/backups", "/run/credentials"):
        assert "-" + path in unit
