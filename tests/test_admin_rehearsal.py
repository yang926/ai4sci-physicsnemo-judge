"""The operator rehearsal can mutate only isolated demo state over HTTP."""
import importlib.util
import json
from pathlib import Path
import secrets
import threading

import pytest

from ai4sci_judge.store import Store
from judge_admin.control import MemoryController
from judge_admin.demo import initialize_demo
from judge_admin.storage import AdminStorage
from judge_admin.web import create_server


SPEC = importlib.util.spec_from_file_location("admin_rehearsal", Path(__file__).parents[1] / "ops/rehearse-admin.py")
rehearsal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rehearsal)


def without_clock(value):
    # The response timestamp advances even when every event record is unchanged.
    return {key: item for key, item in value.items() if key != "server_time"}


@pytest.fixture
def server(tmp_path):
    Store.initialize(tmp_path / "live", steps=2)
    initialize_demo(tmp_path / "demo")
    live = AdminStorage(tmp_path / "live", tmp_path / "live-backups", MemoryController())
    demo = AdminStorage(tmp_path / "demo", tmp_path / "demo-backups", MemoryController())
    password = secrets.token_urlsafe(32)
    httpd = create_server(live, demo, password, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, live, demo, password
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_full_rehearsal_restores_demo_and_never_mutates_live(server, capsys, monkeypatch):
    httpd, live, demo, password = server
    client = rehearsal.Client(f"http://127.0.0.1:{httpd.server_port}")
    original_request = client.request
    requests = []
    def request(path, payload=None):
        requests.append((path, payload is not None))
        return original_request(path, payload)
    monkeypatch.setattr(client, "request", request)
    before = live.status()
    board = demo.store.board()
    rehearsal.run(client, password)
    assert without_clock(live.status()) == without_clock(before)
    assert without_clock(demo.store.board()) == without_clock(board)
    assert not demo.status()["maintenance"]
    assert not live.backups() and not live.audit()
    assert len(demo.backups()) == 5  # Manual baseline plus mandatory backups for 4 destructive actions.
    mutations = [path for path, post in requests if post]
    assert {path for path in mutations if not path.startswith("/api/demo/")} == {"/api/login", "/api/logout"}
    output = capsys.readouterr().out
    assert password not in output and client.csrf not in output
    assert '"live_unchanged": true' in output
    for line in output.splitlines():
        assert isinstance(json.loads(line), dict)


def test_rehearsal_failure_keeps_demo_paused_without_live_repair(server, monkeypatch):
    httpd, live, demo, password = server
    client = rehearsal.Client(f"http://127.0.0.1:{httpd.server_port}")
    original = client.demo_post
    def operation(action, payload=None):
        if action == "restore":
            raise rehearsal.RehearsalError("Simulated restore outage")
        return original(action, payload)
    monkeypatch.setattr(client, "demo_post", operation)
    before = live.status()
    with pytest.raises(rehearsal.RehearsalError, match="outage"):
        rehearsal.run(client, password)
    assert without_clock(live.status()) == without_clock(before)
    assert demo.status()["maintenance"]
    assert demo.status()["participants"] == 30 and demo.status()["submissions"] == 0
    assert not live.audit()


@pytest.mark.parametrize("route", ["/api/reset", "/api/restore", "/api/pause", "/api/resume", "/api/backup"])
def test_script_forbids_every_live_database_write_before_network(route):
    client = rehearsal.Client("http://127.0.0.1:8092")
    with pytest.raises(rehearsal.RehearsalError, match="non-demo"):
        client.request(route, {})


@pytest.mark.parametrize("url", ["https://example.com:8092", "http://example.com:8092", "http://127.0.0.1:8092/path", "http://user:secret@127.0.0.1:8092", "http://127.0.0.1"])
def test_rehearsal_rejects_nonloopback_or_ambiguous_origins(url):
    with pytest.raises(rehearsal.RehearsalError):
        rehearsal.Client(url)
