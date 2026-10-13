"""Administrative operations use only isolated temporary event databases."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid

import pytest

from ai4sci_judge.store import Store
from judge_admin.control import MemoryController, SystemdController, ControlError
from judge_admin.storage import AdminError, AdminStorage


@pytest.fixture
def admin(tmp_path):
    Store.initialize(tmp_path / "event", steps=2)
    return AdminStorage(tmp_path / "event", tmp_path / "backups", MemoryController())


def seed(admin, status="completed"):
    token = secrets.token_urlsafe(32)
    participant = admin.store.provision_workspace("test-org/vm-" + uuid.uuid4().hex, token)["id"]
    admin.store.set_nickname(participant, "Rehearsal " + participant[:6])
    identifier = uuid.uuid4().hex
    _, revision = admin.store.settings()
    with admin.store.connect() as db:
        db.execute("INSERT INTO submissions (id,participant,challenge,source_hash,sources,status,created,score,fingerprint) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (identifier, participant, "1", "test-hash", '{"wave_l1.py":"test source"}', status,
                    time.time(), 84.0 if status == "completed" else None, revision))
    return participant, token


def test_status_has_counts_not_credential_or_source_material(admin):
    participant, token = seed(admin)
    status = admin.status()
    assert status["participants"] == status["named_participants"] == status["completed"] == 1
    assert not status["maintenance"] and not status["ready_to_reset"]
    encoded = json.dumps(status)
    assert token not in encoded and participant not in encoded
    assert hashlib.sha256(token.encode()).hexdigest() not in encoded
    assert "test source" not in encoded


def test_backup_is_consistent_private_and_readonly(admin):
    seed(admin)
    backup = admin.create_backup()
    path = admin.backup_file(backup["id"])
    assert path.stat().st_mode & 0o777 == 0o400
    assert path.parent.stat().st_mode & 0o777 == 0o500
    assert (path.parent / "manifest.json").stat().st_mode & 0o777 == 0o400
    assert hashlib.sha256(path.read_bytes()).hexdigest() == backup["sha256"]
    with sqlite3.connect(path) as snapshot:
        assert snapshot.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert snapshot.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 1
    assert admin.backups()[0]["compatible"]
    assert admin.audit()[0]["action"] == "backup"


def test_live_wal_changes_are_in_backup(admin):
    seed(admin)
    # Keep another connection open so WAL contents are not dependent on a close checkpoint.
    with admin.store.connect() as db:
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("UPDATE submissions SET score=97")
        db.commit()
        backup = admin.create_backup()
        with sqlite3.connect(admin.backup_file(backup["id"])) as snapshot:
            assert snapshot.execute("SELECT score FROM submissions").fetchone()[0] == 97


@pytest.mark.parametrize("scope,phrase", [("scores", "RESET SCORES"), ("event", "RESET EVENT")])
def test_reset_requires_pause_and_exact_confirmation(admin, scope, phrase):
    seed(admin)
    with pytest.raises(AdminError, match="confirmation"):
        admin.reset(scope, phrase.lower())
    with pytest.raises(AdminError, match="Pause"):
        admin.reset(scope, phrase)
    assert admin.status()["submissions"] == 1
    assert admin.backups() == []


def test_score_reset_preserves_participants_bindings_revocations_and_settings(admin):
    participant, token = seed(admin)
    admin.store.revoke_participant(participant)
    before = admin.store.settings()
    admin.pause()
    outcome = admin.reset("scores", "RESET SCORES")
    assert outcome["status"]["submissions"] == 0
    assert outcome["status"]["participants"] == outcome["status"]["bindings"] == 1
    assert outcome["status"]["revoked"] == 1
    assert admin.store.authenticate(token) is None
    assert admin.store.settings() == before
    assert outcome["status"]["maintenance"]
    assert outcome["status"]["controller"]["workers_stopped"]
    assert outcome["backup"]["counts"]["submissions"] == 1


def test_full_reset_preserves_rules_and_external_audit_and_can_restore(admin):
    participant, token = seed(admin)
    before = admin.store.settings()
    admin.pause()
    outcome = admin.reset("event", "RESET EVENT")
    assert outcome["status"]["participants"] == outcome["status"]["bindings"] == outcome["status"]["submissions"] == 0
    assert "master" in outcome["warning"]
    restored = admin.restore(outcome["backup"]["id"], "RESTORE BACKUP")
    assert restored["status"]["participants"] == restored["status"]["completed"] == 1
    assert admin.store.authenticate(token)["id"] == participant
    assert admin.store.settings() == before
    assert restored["status"]["maintenance"]
    actions = [row["action"] for row in admin.audit()]
    assert "reset_completed" in actions and "restore_completed" in actions
    assert len(admin.backups()) == 2
    admin.resume()
    assert not admin.status()["maintenance"]


@pytest.mark.parametrize("pending", ["queued", "running"])
def test_pending_jobs_refuse_reset_without_stopping_workers(admin, pending):
    seed(admin, pending)
    admin.pause()
    with pytest.raises(AdminError, match="queued and running"):
        admin.reset("scores", "RESET SCORES")
    assert not admin.controller.status()["workers_stopped"]
    assert admin.status()["submissions"] == 1
    assert not admin.backups()


def test_pending_job_backups_are_downloadable_but_never_restorable(admin):
    seed(admin, "running")
    backup = admin.create_backup()
    assert not backup["restorable"] and not admin.backups()[0]["compatible"]
    assert admin.backup_file(backup["id"]).is_file()
    with admin.store.connect() as db:
        db.execute("UPDATE submissions SET status='completed',score=1")
    admin.pause()
    with pytest.raises(AdminError, match="queued or running"):
        admin.restore(backup["id"], "RESTORE BACKUP")


def test_failed_auto_backup_prevents_reset(admin, monkeypatch):
    seed(admin)
    admin.pause()
    def fail(_):
        raise OSError("Backup disk full")
    monkeypatch.setattr(admin, "_create_backup", fail)
    with pytest.raises(OSError, match="disk full"):
        admin.reset("event", "RESET EVENT")
    assert admin.status()["submissions"] == admin.status()["participants"] == 1
    assert admin.status()["maintenance"]


def test_failed_audit_prevents_reset(admin, monkeypatch):
    seed(admin)
    admin.pause()
    monkeypatch.setattr(admin, "_audit", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("audit disk full")))
    with pytest.raises(OSError, match="audit disk full"):
        admin.reset("scores", "RESET SCORES")
    assert admin.status()["submissions"] == 1


@pytest.mark.parametrize("identifier", ["../judge.sqlite3", "/etc/passwd", "a" * 500, "", None, 123])
def test_backup_ids_are_not_paths(admin, identifier):
    with pytest.raises(AdminError):
        admin.backup_file(identifier)
    with pytest.raises(AdminError):
        admin.restore(identifier, "RESTORE BACKUP")


def test_corrupted_backup_refused_before_current_data_changes(admin):
    seed(admin)
    backup = admin.create_backup()
    path = admin.backup_file(backup["id"])
    path.chmod(0o600)
    with path.open("r+b") as stream:
        stream.write(b"BROKEN")
    admin.pause()
    with pytest.raises(AdminError, match="hash"):
        admin.restore(backup["id"], "RESTORE BACKUP")
    assert admin.status()["submissions"] == 1
    assert not admin.backups()


def test_cross_state_restore_refused_even_same_scoring_version(admin, tmp_path):
    seed(admin)
    backup = admin.create_backup()
    Store.initialize(tmp_path / "other-event", steps=2)
    other = AdminStorage(tmp_path / "other-event", admin.backup_root, MemoryController())
    other.pause()
    with pytest.raises(AdminError, match="belong"):
        other.restore(backup["id"], "RESTORE BACKUP")
    assert other.status()["submissions"] == 0


def test_changed_rules_refuse_restore_even_if_manifest_hash_updated(admin):
    seed(admin)
    backup = admin.create_backup()
    path = admin.backup_file(backup["id"])
    path.parent.chmod(0o700)
    path.chmod(0o600)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE settings SET fingerprint='different'")
    manifest_path = path.parent / "manifest.json"
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text())
    manifest["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    admin.pause()
    with pytest.raises(AdminError, match="fingerprint"):
        admin.restore(backup["id"], "RESTORE BACKUP")
    assert not admin.backups()[0]["compatible"]
    assert admin.status()["submissions"] == 1


def test_changed_schema_refuse_restore(admin):
    backup = admin.create_backup()
    path = admin.backup_file(backup["id"])
    path.parent.chmod(0o700)
    path.chmod(0o600)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE surprise (value TEXT)")
    manifest_path = path.parent / "manifest.json"
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text())
    manifest.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
    manifest_path.write_text(json.dumps(manifest))
    admin.pause()
    with pytest.raises(AdminError, match="schema"):
        admin.restore(backup["id"], "RESTORE BACKUP")


def test_symlink_state_and_backup_roots_refused(admin, tmp_path):
    link = tmp_path / "linked-state"
    link.symlink_to(admin.state, target_is_directory=True)
    with pytest.raises(AdminError, match="Symlinks"):
        AdminStorage(link, tmp_path / "other-backups", MemoryController())
    link.unlink()
    link.symlink_to(admin.backup_root, target_is_directory=True)
    with pytest.raises(AdminError, match="Symlinks"):
        AdminStorage(admin.state, link, MemoryController())


def test_world_readable_storage_refused(admin):
    admin.backup_root.chmod(0o755)
    with pytest.raises(AdminError, match="owner-only"):
        AdminStorage(admin.state, admin.backup_root, MemoryController())
    admin.backup_root.chmod(0o700)


def test_backup_inside_live_state_refused(admin):
    with pytest.raises(AdminError, match="outside"):
        AdminStorage(admin.state, admin.state / "backup", MemoryController())


def test_active_state_inside_backup_root_refused(admin):
    with pytest.raises(AdminError, match="nested"):
        AdminStorage(admin.state, admin.state.parent, MemoryController())


def test_hard_linked_backup_refused(admin, tmp_path):
    backup = admin.create_backup()
    path = admin.backup_file(backup["id"])
    os.link(path, tmp_path / "linked.sqlite3")
    with pytest.raises(AdminError, match="Hard-linked"):
        admin.backup_file(backup["id"])


def test_backup_list_caches_validation_but_download_always_checks(admin, monkeypatch):
    backup = admin.create_backup()
    import judge_admin.storage as storage
    original = storage._digest
    checks = []
    def digest(path):
        checks.append(path)
        return original(path)
    monkeypatch.setattr(storage, "_digest", digest)
    assert admin.backups()[0]["compatible"]
    assert len(checks) == 1
    admin.backups()
    assert len(checks) == 1
    admin.backup_file(backup["id"])
    assert len(checks) == 2


def test_parallel_backups_are_serialized_and_audit_intact(admin):
    seed(admin)
    with ThreadPoolExecutor(max_workers=3) as pool:
        backups = list(pool.map(lambda _: admin.create_backup(), range(3)))
    assert len({row["id"] for row in backups}) == 3
    assert len(admin.backups()) == 3
    assert len(admin.audit()) == 3


def test_controller_failure_does_not_delete_data(admin, monkeypatch):
    seed(admin)
    admin.pause()
    monkeypatch.setattr(admin.controller, "quiesce", lambda: {"intake_paused": True, "workers_stopped": False})
    with pytest.raises(AdminError, match="writers"):
        admin.reset("scores", "RESET SCORES")
    assert admin.status()["submissions"] == 1


def test_fixed_controller_never_uses_shell_or_user_service_names(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        class Result:
            stdout = '{"intake_paused":true,"workers_stopped":true,"services":{}}'
        return Result()
    monkeypatch.setattr("judge_admin.control.subprocess.run", run)
    control = SystemdController()
    assert control.pause()["intake_paused"]
    args, options = calls[0]
    assert args == ["/usr/bin/sudo", "-n", "/usr/local/sbin/ai4sci-judge-control", "pause"]
    assert not options.get("shell")
    with pytest.raises(ControlError):
        control._call("stop ssh")
