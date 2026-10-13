"""Challenge operations exercise real handlers and temporary SQLite without sockets."""
from email.parser import BytesParser
import hashlib
import io
import json
import sqlite3
import time
from types import SimpleNamespace
import uuid

import pytest

from ai4sci_judge.store import Store
from judge_admin.control import MemoryController
from judge_admin.storage import AdminError, AdminStorage
from judge_admin.web import create_server


@pytest.fixture
def admin(tmp_path, monkeypatch):
    contexts = {}
    for name in ("live", "demo"):
        store = Store.initialize(tmp_path / name, steps=2)
        contexts[name] = AdminStorage(store.directory, tmp_path / (name + "-backups"), MemoryController())

    # Dispatch through the actual HTTP handler while forbidding any listener.
    def fake_server(address, handler):
        return SimpleNamespace(server_port=8092, handler=handler)

    monkeypatch.setattr("judge_admin.web.ThreadingHTTPServer", fake_server)
    password = "isolated-challenge-admin-test-password"
    server = create_server(contexts["live"], contexts["demo"], password)
    credentials = {}

    class RequestSocket:
        def __init__(self, data):
            self.data = data
            self.response = bytearray()

        def makefile(self, *_args):
            return io.BytesIO(self.data)

        def sendall(self, data):
            self.response.extend(data)

        def settimeout(self, _timeout):
            pass

    def call(path, payload=None, *, authenticated=True, headers=None):
        method = "GET" if payload is None else "POST"
        body = b"" if payload is None else json.dumps(payload).encode()
        fields = {"Host": "127.0.0.1:8092", "Origin": "http://127.0.0.1:8092",
                  "Connection": "close", "Content-Type": "application/json", "Content-Length": str(len(body))}
        if authenticated:
            fields.update(credentials)
        fields.update(headers or {})
        request = (f"{method} {path} HTTP/1.1\r\n" +
                   "".join(f"{key}: {value}\r\n" for key, value in fields.items()) + "\r\n").encode() + body
        sock = RequestSocket(request)
        server.handler(sock, ("127.0.0.1", 1234), server)
        head, _, response_body = bytes(sock.response).partition(b"\r\n\r\n")
        status, _, raw_headers = head.partition(b"\r\n")
        response_headers = BytesParser().parsebytes(raw_headers)
        return int(status.split()[1]), json.loads(response_body), response_headers

    def login():
        code, payload, headers = call("/api/login", {"password": password}, authenticated=False)
        assert code == 200
        credentials.update({"Cookie": headers["Set-Cookie"].split(";", 1)[0],
                            "X-CSRF-Token": payload["csrf_token"]})

    return {**contexts, "call": call, "login": login}


def seed(storage, challenge="1", status="completed"):
    token = "challenge-admin-test-credential-" + uuid.uuid4().hex
    participant = storage.store.provision_workspace("fixture/" + uuid.uuid4().hex, token)["id"]
    storage.store.set_nickname(participant, "Learner " + participant[:8])
    identifier = uuid.uuid4().hex
    with storage.store.connect() as db:
        db.execute("INSERT INTO submissions "
                   "(id,participant,challenge,source_hash,sources,status,created,score,fingerprint) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (identifier, participant, challenge, "fixture-source", '{}', status,
                    time.time() - 60, 72.5 if status == "completed" else None, storage.store.settings()[1]))
    return participant, token, identifier


def lifecycle_rows(storage):
    with storage.store.connect() as db:
        return [tuple(row) for row in db.execute("SELECT * FROM challenge_lifecycle ORDER BY challenge")]


@pytest.mark.parametrize("prefix", ["/api", "/api/demo"])
def test_lifecycle_mutations_require_login_origin_and_csrf(admin, prefix):
    call = admin["call"]
    path = prefix + "/challenges/action"
    payload = {"challenge": "1", "action": "close"}
    assert call(path, payload)[0] == 401
    admin["login"]()
    for headers in ({"X-CSRF-Token": ""}, {"Origin": "http://elsewhere.example"}, {"Host": "elsewhere.example"}):
        assert call(path, payload, headers=headers)[0] == 403
    for storage in (admin["live"], admin["demo"]):
        assert storage.status()["lifecycle"]["1"]["state"] == "open"
        assert storage.audit() == []


@pytest.mark.parametrize("payload", [
    {}, {"challenge": "1"}, {"challenge": "1", "action": "close", "state": "demo"},
    {"challenge": "all", "action": "close"}, {"challenge": "0", "action": "close"},
    {"challenge": 1, "action": "close"}, {"challenge": "1", "action": "open"},
    {"challenge": "1", "action": "pause"}, {"challenge": "1", "action": None},
])
def test_action_schema_and_allowlists_reject_invalid_requests(admin, payload):
    admin["login"]()
    assert admin["call"]("/api/challenges/action", payload)[0] == 400
    assert admin["live"].audit() == []
    assert all(row["state"] == "open" for row in admin["live"].status()["lifecycle"].values())


def test_status_and_actions_are_challenge_scoped_and_never_control_services(admin, monkeypatch):
    live = admin["live"]
    for action in ("pause", "resume", "quiesce"):
        monkeypatch.setattr(live.controller, action, lambda: pytest.fail("Challenge action must not control services"))
    admin["login"]()
    code, status, _ = admin["call"]("/api/status")
    assert code == 200 and status["server_time"] > 0 and set(status["lifecycle"]) == set("1234")
    for action, expected in (("schedule_close", "closing"), ("close", "closed"), ("finalize", "finalized")):
        code, result, _ = admin["call"]("/api/challenges/action", {"challenge": "2", "action": action})
        assert code == 200
        assert result["result"]["lifecycle"]["2"]["state"] == expected
        for challenge in ("1", "3", "4"):
            assert result["result"]["lifecycle"][challenge]["state"] == "open"
    assert not live.status()["maintenance"] and not live.status()["controller"]["workers_stopped"]
    audit = list(reversed(live.audit()))
    assert [row["action"] for row in audit] == ["challenge_action_requested", "challenge_action_completed"] * 3
    assert [row["transition"] for row in audit] == ["schedule_close"] * 2 + ["close"] * 2 + ["finalize"] * 2
    assert all(row["challenge"] == "2" for row in audit)


def test_demo_lifecycle_actions_do_not_touch_live_data_or_audit(admin):
    seed(admin["live"])
    before = lifecycle_rows(admin["live"])
    admin["login"]()
    for action in ("schedule_close", "close", "finalize"):
        assert admin["call"]("/api/demo/challenges/action", {"challenge": "1", "action": action})[0] == 200
    assert lifecycle_rows(admin["live"]) == before
    assert admin["live"].audit() == []
    assert admin["live"].status()["submissions"] == 1
    assert admin["demo"].status()["lifecycle"]["1"]["state"] == "finalized"


@pytest.mark.parametrize("pending", ["queued", "running", "system_error"])
def test_blocked_finalization_records_request_without_completion(admin, pending):
    live = admin["live"]
    seed(live, status=pending)
    live.challenge_action("1", "close")
    admin["login"]()
    assert admin["call"]("/api/challenges/action", {"challenge": "1", "action": "finalize"})[0] == 400
    assert live.status()["lifecycle"]["1"]["state"] == "closed"
    entries = [row for row in live.audit() if row.get("transition") == "finalize"]
    assert len(entries) == 1 and entries[0]["action"] == "challenge_action_requested"


def test_retry_requeues_only_selected_errors_and_audits_count(admin):
    live = admin["live"]
    _, _, original = seed(live, status="system_error")
    seed(live, status="completed")
    seed(live, challenge="2", status="system_error")
    live.challenge_action("1", "close")
    with live.store.connect() as db:
        before = tuple(db.execute("SELECT participant,sources,created,fingerprint FROM submissions WHERE id=?", (original,)).fetchone())
    admin["login"]()
    code, result, _ = admin["call"]("/api/challenges/action", {"challenge": "1", "action": "retry_errors"})
    assert code == 200 and result["result"]["retried"] == 1
    assert result["result"]["lifecycle"]["1"]["state"] == "closed"
    with live.store.connect() as db:
        assert db.execute("SELECT status FROM submissions WHERE id=?", (original,)).fetchone()[0] == "queued"
        assert tuple(db.execute("SELECT participant,sources,created,fingerprint FROM submissions WHERE id=?", (original,)).fetchone()) == before
        assert db.execute("SELECT status FROM submissions WHERE challenge='2'").fetchone()[0] == "system_error"
        assert db.execute("SELECT COUNT(*) FROM submissions WHERE status='completed'").fetchone()[0] == 1
    assert live.audit()[0]["action"] == "challenge_action_completed"
    assert live.audit()[0]["retried"] == 1


def test_audit_write_failure_prevents_lifecycle_change(admin, monkeypatch):
    live = admin["live"]
    def fail(*_args, **_kwargs):
        raise OSError("audit unavailable")
    monkeypatch.setattr(live, "_audit", fail)
    with pytest.raises(OSError, match="audit unavailable"):
        live.challenge_action("1", "close")
    assert live.status()["lifecycle"]["1"]["state"] == "open"


@pytest.mark.parametrize("scope,confirmation", [("scores", "RESET SCORES"), ("event", "RESET EVENT")])
def test_guarded_reset_reopens_and_restore_recovers_complete_lifecycle(admin, scope, confirmation):
    live = admin["live"]
    participant, token, _ = seed(live)
    live.store.revoke_participant(participant)
    live.challenge_action("1", "close")
    live.challenge_action("1", "finalize")
    live.challenge_action("2", "schedule_close")
    live.challenge_action("3", "close")
    before = lifecycle_rows(live)
    with pytest.raises(AdminError, match="Pause"):
        live.reset(scope, confirmation)
    live.pause()
    cleared = live.reset(scope, confirmation)
    assert cleared["status"]["submissions"] == 0
    assert cleared["status"]["participants"] == (1 if scope == "scores" else 0)
    assert cleared["status"]["bindings"] == (1 if scope == "scores" else 0)
    assert cleared["status"]["revoked"] == (1 if scope == "scores" else 0)
    with live.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM challenge_lifecycle WHERE state='open' AND deadline IS NULL AND finalized_at IS NULL AND snapshot IS NULL").fetchone()[0] == 4
    restored = live.restore(cleared["backup"]["id"], "RESTORE BACKUP")
    assert lifecycle_rows(live) == before
    assert restored["status"]["submissions"] == restored["status"]["participants"] == restored["status"]["revoked"] == 1
    assert live.store.authenticate(token) is None
    live.resume()
    assert live.status()["lifecycle"]["1"]["state"] == "finalized"
    assert live.status()["lifecycle"]["3"]["state"] == "closed"


@pytest.mark.parametrize("damage,message", [("legacy", "predates Challenge lifecycle"),
                                          ("missing_record", "lifecycle records are incomplete")])
def test_invalid_lifecycle_backup_is_explicitly_incompatible_and_cannot_reopen(admin, damage, message):
    live = admin["live"]
    live.challenge_action("1", "close")
    saved = live.create_backup()
    path = live.backup_file(saved["id"])
    path.parent.chmod(0o700)
    path.chmod(0o600)
    with sqlite3.connect(path) as db:
        if damage == "legacy":
            db.execute("DROP TABLE challenge_lifecycle")
        else:
            db.execute("DELETE FROM challenge_lifecycle WHERE challenge='1'")
    manifest_path = path.parent / "manifest.json"
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text())
    manifest.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
    manifest_path.write_text(json.dumps(manifest))
    assert not live.backups()[0]["compatible"]
    live.pause()
    with pytest.raises(AdminError, match=message):
        live.restore(saved["id"], "RESTORE BACKUP")
    assert live.status()["lifecycle"]["1"]["state"] == "closed"
