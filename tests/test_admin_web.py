"""Operator authentication, CSRF boundary and isolated rehearsal lifecycle."""
import http.cookiejar
import json
import threading
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pytest

from ai4sci_judge.store import Store
from judge_admin.control import MemoryController
from judge_admin.demo import initialize_demo
from judge_admin.storage import AdminStorage
from judge_admin.web import create_server


@pytest.fixture
def admin(tmp_path):
    live_store = Store.initialize(tmp_path / "live", steps=2)
    live_store.add_participant("Real instructor fixture")
    demo_store = initialize_demo(tmp_path / "demo")
    live = AdminStorage(live_store.directory, tmp_path / "live-backups", MemoryController())
    demo = AdminStorage(demo_store.directory, tmp_path / "demo-backups", MemoryController())
    password = "test-only-administrator-password-not-a-production-secret"
    server = create_server(live, demo, password, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf = None

    def call(path, data=None, *, overrides=None, raw=False):
        headers = {"Origin": base}
        if data is not None:
            headers["Content-Type"] = "application/json"
            if csrf:
                headers["X-CSRF-Token"] = csrf
        headers.update(overrides or {})
        request = Request(base + path, data=None if data is None else json.dumps(data).encode(), headers=headers)
        with opener.open(request, timeout=20) as response:
            return response.read() if raw else json.load(response)

    def login():
        nonlocal csrf
        csrf = call("/api/login", {"password": password})["csrf_token"]

    yield {"live": live, "demo": demo, "call": call, "login": login, "server": server, "base": base}
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_admin_pages_public_but_data_downloads_and_demo_require_login(admin):
    call = admin["call"]
    assert b"<html" in call("/", raw=True)
    for path in ("/api/status", "/api/demo/status", "/demo", "/demo/api/board", "/api/backups/anything"):
        with pytest.raises(HTTPError) as error:
            call(path)
        assert error.value.code == 401
    with pytest.raises(HTTPError) as error:
        call("/api/login", {"password": "wrong"})
    assert error.value.code == 401
    admin["login"]()
    status = call("/api/status")
    assert status["mode"] == "live" and status["participants"] == 1
    assert "token_hash" not in json.dumps(status)
    assert status["csrf_token"]
    assert call("/api/demo/status")["participants"] == 30
    assert b"DEMO" in call("/demo", raw=True)
    assert len(call("/demo/api/board")["participants"]) == 30
    call("/api/logout", {})
    with pytest.raises(HTTPError) as error:
        call("/api/status")
    assert error.value.code == 401


def test_origin_host_csrf_and_request_schema_fail_closed(admin):
    call = admin["call"]
    with pytest.raises(HTTPError) as error:
        call("/api/login", {"password": "anything"}, overrides={"Origin": "https://other.example"})
    assert error.value.code == 403
    admin["login"]()
    for overrides in ({"X-CSRF-Token": ""}, {"Origin": "http://localhost:1"}, {"Host": "evil.example"}):
        with pytest.raises(HTTPError) as error:
            call("/api/backup", {}, overrides=overrides)
        assert error.value.code == 403
    with pytest.raises(HTTPError) as error:
        call("/api/reset", {"scope": "event", "confirmation": "RESET EVENT", "state": "/anything"})
    assert error.value.code == 400
    assert admin["live"].status()["participants"] == 1


def test_real_and_demo_backups_reset_restore_are_separate(admin):
    call = admin["call"]
    admin["login"]()
    before = admin["live"].store.path.read_bytes()
    with pytest.raises(HTTPError):
        call("/api/demo/reset", {"scope": "scores", "confirmation": "RESET SCORES"})
    call("/api/demo/pause", {})
    call("/api/demo/backup", {})
    saved = call("/api/demo/status")["backups"][0]
    data = call("/api/demo/backups/" + saved["id"], raw=True)
    assert data.startswith(b"SQLite format 3")
    call("/api/demo/reset", {"scope": "scores", "confirmation": "RESET SCORES"})
    cleared = call("/api/demo/status")
    assert cleared["participants"] == 30 and cleared["submissions"] == 0
    assert cleared["maintenance"]
    call("/api/demo/restore", {"backup_id": saved["id"], "confirmation": "RESTORE BACKUP"})
    assert call("/api/demo/status")["submissions"] > 100
    call("/api/demo/reset", {"scope": "event", "confirmation": "RESET EVENT"})
    assert call("/api/demo/status")["participants"] == 0
    call("/api/demo/restore", {"backup_id": saved["id"], "confirmation": "RESTORE BACKUP"})
    call("/api/demo/resume", {})
    assert not call("/api/demo/status")["maintenance"]
    assert admin["live"].store.path.read_bytes() == before
    assert call("/api/status")["participants"] == 1
    with pytest.raises(HTTPError):
        call("/api/restore", {"backup_id": saved["id"], "confirmation": "RESTORE BACKUP"})


def test_login_attempts_are_bounded(admin):
    for _ in range(10):
        with pytest.raises(HTTPError) as error:
            admin["call"]("/api/login", {"password": "wrong"})
        assert error.value.code == 401
    with pytest.raises(HTTPError) as error:
        admin["call"]("/api/login", {"password": "wrong"})
    assert error.value.code == 429
