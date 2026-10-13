"""Brev-facing administrator listener, using only disposable fixture databases."""
from http.client import HTTPConnection, RemoteDisconnected
from http.cookies import SimpleCookie
import json
import socket
import threading
import time

import pytest

from ai4sci_judge.store import Store
from judge_admin.control import MemoryController
from judge_admin.storage import AdminStorage
from judge_admin.web import create_server, validate_secure_link


HOST = "admin.example.test"
PASSWORD = "fixture-only-administrator-password-123456"


def free_port():
    with socket.socket() as temporary:
        temporary.bind(("127.0.0.1", 0))
        return temporary.getsockname()[1]


def settings(**changes):
    return {"hostname": HOST, "bind_address": "127.0.0.1", "port": 8094,
            "allowed_peers": ["127.0.0.1"], **changes}


@pytest.fixture
def admin(tmp_path):
    live_store = Store.initialize(tmp_path / "live", steps=2)
    live_store.add_participant("Disposable live fixture")
    demo_store = Store.initialize(tmp_path / "demo", steps=2)
    demo_store.add_participant("Disposable demo fixture")
    live = AdminStorage(live_store.directory, tmp_path / "live-backups", MemoryController())
    demo = AdminStorage(demo_store.directory, tmp_path / "demo-backups", MemoryController())
    server = create_server(live, demo, PASSWORD, port=0, secure_link=settings(port=free_port()))
    threads = []
    for listener in (server, server.secure_server):
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)

    def call(path, data=None, *, secure=True, headers=None, duplicate=None):
        listener = server.secure_server if secure else server
        host = HOST if secure else f"127.0.0.1:{listener.server_port}"
        request_headers = {"Host": host}
        body = None if data is None else json.dumps(data).encode()
        if data is not None:
            request_headers.update({"Content-Type": "application/json", "Content-Length": str(len(body)),
                                    "Origin": ("https://" if secure else "http://") + host})
        for name, value in (headers or {}).items():
            if value is None:
                request_headers.pop(name, None)
            else:
                request_headers[name] = value
        connection = HTTPConnection("127.0.0.1", listener.server_port, timeout=5)
        try:
            connection.putrequest("GET" if data is None else "POST", path, skip_host=True)
            for name, value in request_headers.items():
                connection.putheader(name, value)
            for name, value in (duplicate or []):
                connection.putheader(name, value)
            connection.endheaders(body)
            response = connection.getresponse()
            content = response.read()
            return response.status, dict(response.getheaders()), content
        finally:
            connection.close()

    def login(secure=True):
        status, headers, body = call("/api/login", {"password": PASSWORD}, secure=secure)
        assert status == 200
        return headers["Set-Cookie"].split(";", 1)[0], json.loads(body)["csrf_token"], headers

    yield {"server": server, "call": call, "login": login, "live": live, "demo": demo}
    for listener in (server.secure_server, server):
        listener.shutdown()
        listener.server_close()
    for thread in threads:
        thread.join(timeout=5)


@pytest.mark.parametrize("change", [
    {"hostname": "https://admin.example.test"}, {"hostname": "Admin.example.test"},
    {"hostname": "*.example.test"}, {"hostname": "admin.example.test:443"},
    {"hostname": "127.0.0.1"}, {"hostname": "admin.local"}, {"hostname": "admin.localhost"},
    {"hostname": "localhost"}, {"hostname": "admin.example.test/"}, {"hostname": "admin.example.test."},
    {"bind_address": "0.0.0.0"}, {"bind_address": "100.64.0.0/10"},
    {"bind_address": "8.8.8.8"}, {"bind_address": "::1"}, {"bind_address": "127.0.0.2"},
    {"bind_address": "100.128.0.1"}, {"allowed_peers": []}, {"allowed_peers": "127.0.0.1"},
    {"allowed_peers": ["100.64.0.0/10"]}, {"allowed_peers": ["0.0.0.0"]},
    {"allowed_peers": ["8.8.8.8"]}, {"allowed_peers": ["127.0.0.1", "127.0.0.1"]},
    {"allowed_peers": ["::1"]}, {"allowed_peers": [None]},
    {"port": 0}, {"port": 65536}, {"port": True}, {"port": "8094"},
    {"unknown": True},
])
def test_invalid_secure_link_config_fails_closed(change):
    with pytest.raises(ValueError):
        validate_secure_link(settings(**change))


@pytest.mark.parametrize("value", [[], {}, "enabled", {"hostname": HOST}])
def test_incomplete_secure_link_config_is_rejected(value):
    with pytest.raises(ValueError):
        validate_secure_link(value)


def test_valid_private_gateway_configuration_and_opt_in_default():
    value = settings(bind_address="100.64.0.10", allowed_peers=["100.64.0.21", "100.64.0.10"])
    assert validate_secure_link(value) == value
    assert validate_secure_link(None) is None
    server = create_server(None, None, PASSWORD, port=0)
    try:
        assert server.secure_server is None
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_login_cookie_is_secure_only_on_external_listener_and_ports_do_not_mix(admin):
    external_cookie, _, external_headers = admin["login"]()
    local_cookie, _, local_headers = admin["login"](secure=False)
    cookie = SimpleCookie()
    cookie.load(external_headers["Set-Cookie"])
    morsel = next(iter(cookie.values()))
    assert morsel["secure"] and morsel["httponly"]
    assert morsel["samesite"] == "Strict" and morsel["path"] == "/"
    assert morsel["max-age"] == "28800" and not morsel["domain"]
    assert "; Secure" not in local_headers["Set-Cookie"]
    assert external_cookie.split("=")[0] != local_cookie.split("=")[0]
    for secure, good, other in [(True, external_cookie, local_cookie), (False, local_cookie, external_cookie)]:
        status, _, body = admin["call"]("/api/status", secure=secure, headers={"Cookie": good})
        assert status == 200 and json.loads(body)["participants"] == 1
        assert admin["call"]("/api/status", secure=secure, headers={"Cookie": other})[0] == 401


def test_external_listener_retains_password_csrf_and_logout(admin):
    call = admin["call"]
    assert call("/api/status")[0] == 401
    assert call("/api/login", {"password": "wrong"})[0] == 401
    cookie, csrf, _ = admin["login"]()
    for token in (None, "wrong"):
        assert call("/api/logout", {}, headers={"Cookie": cookie, "X-CSRF-Token": token})[0] == 403
    status, headers, _ = call("/api/logout", {}, headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 200 and "; Max-Age=0; Secure" in headers["Set-Cookie"]
    assert call("/api/status", headers={"Cookie": cookie})[0] == 401


@pytest.mark.parametrize("headers", [
    {"Host": "evil.example"}, {"Host": HOST + ":443"},
    {"Host": "localhost:8094"}, {"Host": "127.0.0.1:8094"},
    {"Origin": "http://" + HOST}, {"Origin": "https://evil.example"},
    {"Origin": "null"}, {"Origin": None},
    {"Host": "evil.example", "X-Forwarded-Host": HOST, "X-Forwarded-Proto": "https"},
])
def test_login_requires_exact_host_and_https_origin(admin, headers):
    assert admin["call"]("/api/login", {"password": PASSWORD}, headers=headers)[0] == 403


@pytest.mark.parametrize("name,value", [
    ("Host", HOST), ("Origin", "https://" + HOST), ("Content-Length", "2"),
    ("Cookie", "x=y"), ("X-CSRF-Token", "wrong"), ("Transfer-Encoding", "chunked"),
    ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Mode", "cors"), ("Sec-Fetch-Dest", "empty"),
])
def test_duplicate_authority_and_framing_headers_are_rejected(admin, name, value):
    assert admin["call"]("/api/login", {"password": PASSWORD}, headers={name: value},
                         duplicate=[(name, value)])[0] == 403


def test_local_host_and_origin_policy_is_preserved(admin):
    call = admin["call"]
    assert call("/", secure=False)[0] == 200
    assert call("/", secure=False, headers={"Host": HOST})[0] == 403
    assert call("/", secure=False, headers={"Sec-Fetch-Site": "cross-site",
                                           "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})[0] == 403


@pytest.mark.parametrize("path", ["/", "/admin", "/demo-admin"])
def test_brev_login_return_allows_only_top_level_html_then_same_origin_session(admin, path):
    cookie, _, _ = admin["login"]()
    # A Strict cookie is absent on the cross-site redirect; the shell does not need it.
    status, _, body = admin["call"](path, headers={"Sec-Fetch-Site": "cross-site",
                                                     "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})
    assert status == 200 and b"<html" in body
    assert admin["call"]("/admin.js", headers={"Sec-Fetch-Site": "same-origin"})[0] == 200
    status, _, body = admin["call"]("/api/status", headers={"Cookie": cookie, "Sec-Fetch-Site": "same-origin"})
    assert status == 200 and json.loads(body)["participants"] == 1


@pytest.mark.parametrize("path", ["/api/status", "/api/demo/status", "/api/backups/anything",
                                 "/demo", "/demo/api/board", "/admin.js", "/brand-logo.svg"])
def test_cross_site_non_html_entry_is_rejected_even_with_session(admin, path):
    cookie, _, _ = admin["login"]()
    assert admin["call"](path, headers={"Cookie": cookie, "Sec-Fetch-Site": "cross-site",
                                       "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})[0] == 403


@pytest.mark.parametrize("headers", [{}, {"Sec-Fetch-Mode": "navigate"}, {"Sec-Fetch-Dest": "document"},
                                    {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "document"},
                                    {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"}])
def test_cross_site_shell_requires_navigation_and_document(admin, headers):
    assert admin["call"]("/admin", headers={"Sec-Fetch-Site": "cross-site", **headers})[0] == 403


def test_cross_site_post_denied_even_with_correct_session_csrf_and_origin(admin):
    cookie, csrf, _ = admin["login"]()
    assert admin["call"]("/api/logout", {}, headers={"Cookie": cookie, "X-CSRF-Token": csrf,
             "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})[0] == 403


def test_disallowed_real_peer_closed_before_handler_or_worker_even_with_forwarded_header(admin):
    server = admin["server"].secure_server
    server.allowed_peers = frozenset({"100.64.0.21"})
    before = server.slots._value
    with pytest.raises((RemoteDisconnected, ConnectionResetError, BrokenPipeError)):
        admin["call"]("/", headers={"X-Forwarded-For": "100.64.0.21"})
    assert server.slots._value == before


def test_worker_limit_rejects_excess_connections_without_reading_http(admin):
    server = admin["server"].secure_server
    server.slots = threading.BoundedSemaphore(1)
    first = socket.create_connection(server.server_address, timeout=3)
    try:
        deadline = time.monotonic() + 3
        while server.slots._value and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.slots._value == 0
        with socket.create_connection(server.server_address, timeout=3) as excess:
            assert excess.recv(1) == b""
    finally:
        first.close()
    deadline = time.monotonic() + 3
    while server.slots._value == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.slots._value == 1
    assert admin["call"]("/")[0] == 200


def test_demo_operations_and_backup_download_use_existing_handler_only(admin):
    cookie, csrf, _ = admin["login"]()
    headers = {"Cookie": cookie, "X-CSRF-Token": csrf}
    call = admin["call"]
    before = admin["live"].store.path.read_bytes()
    assert call("/api/demo/pause", {}, headers=headers)[0] == 200
    status, _, body = call("/api/demo/backup", {}, headers=headers)
    assert status == 200
    backup = json.loads(body)["result"]["id"]
    status, response_headers, body = call("/api/demo/backups/" + backup, headers=headers)
    assert status == 200 and body.startswith(b"SQLite format 3")
    assert response_headers["Content-Type"] == "application/octet-stream"
    assert response_headers["Cache-Control"] == "no-store"
    assert response_headers["Content-Disposition"].endswith(f'{backup}.sqlite3"')
    assert call("/api/demo/reset", {"scope": "event", "confirmation": "RESET EVENT"}, headers=headers)[0] == 200
    assert admin["demo"].status()["participants"] == 0
    assert call("/api/demo/restore", {"backup_id": backup, "confirmation": "RESTORE BACKUP"}, headers=headers)[0] == 200
    assert admin["demo"].status()["participants"] == 1
    assert call("/api/demo/resume", {}, headers=headers)[0] == 200
    assert admin["live"].store.path.read_bytes() == before


def test_second_bind_failure_closes_local_socket():
    primary_port = free_port()
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with pytest.raises(OSError):
            create_server(None, None, PASSWORD, port=primary_port,
                          secure_link=settings(port=occupied.getsockname()[1]))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", primary_port))


def test_reusing_primary_listener_port_is_rejected_and_closed():
    port = free_port()
    with pytest.raises(ValueError, match="different ports"):
        create_server(None, None, PASSWORD, port=port, secure_link=settings(port=port))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
