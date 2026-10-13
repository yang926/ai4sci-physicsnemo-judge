"""Durable operator credentials, authenticated rotation and failure boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pytest

from judge_admin.auth import CredentialError, PasswordStore, validate_password
from judge_admin.web import create_server


OLD = "fixture-old-password"
NEW = "fixture-new-password"


@pytest.fixture
def store(tmp_path):
    return PasswordStore.initialize(tmp_path / "credential.json", OLD)


class Client:
    def __init__(self, base):
        self.base, self.csrf = base, ""
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def call(self, path, data=None, overrides=None):
        headers = {"Origin": self.base, "Content-Type": "application/json", "X-CSRF-Token": self.csrf}
        headers.update(overrides or {})
        request = Request(self.base + path, headers=headers,
                          data=None if data is None else json.dumps(data).encode())
        try:
            response = self.opener.open(request, timeout=20)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, json.load(response), response.headers

    def login(self, password=OLD):
        code, result, _ = self.call("/api/login", {"password": password})
        if code == 200:
            self.csrf = result["csrf_token"]
        return code

    def change(self, current=OLD, new=NEW, confirmation=NEW, **kwargs):
        return self.call("/api/password/change", {"current_password": current,
                         "new_password": new, "confirmation": confirmation}, **kwargs)


@contextmanager
def running(store):
    storage = SimpleNamespace(status=lambda: {}, backups=lambda: [], audit=lambda: [])
    server = create_server(storage, storage, None, port=0, credential_store=store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_store_is_salted_private_and_survives_restart(store):
    assert store.verify(OLD) and not store.verify(NEW)
    before = store.path.read_bytes()
    assert OLD.encode() not in before
    assert store.path.stat().st_mode & 0o777 == 0o600
    store.replace(NEW)
    assert store.path.read_bytes() != before
    restarted = PasswordStore(store.path)
    assert restarted.verify(NEW) and not restarted.verify(OLD)
    with pytest.raises(CredentialError):
        PasswordStore.initialize(store.path, OLD)
    assert restarted.verify(NEW)


@pytest.mark.parametrize("candidate", [None, "", "abc", "a" * 129, "hello\nworld", "test\x00value"])
def test_password_policy_rejects_invalid_values(candidate):
    with pytest.raises(ValueError):
        validate_password(candidate)


def test_inner_pin_and_unicode_passwords_are_supported(store):
    for value in ("4938", "한글암호확인", " spaces allowed ", "a" * 128):
        store.replace(value)
        assert store.verify(value)


@pytest.mark.parametrize("bad", [b"{", b"[]", b"{}", b" " * 5000,
                                   b'{"version":true}', b'\xff'])
def test_corrupt_or_oversized_store_never_falls_back(store, bad):
    store.path.write_bytes(bad)
    with pytest.raises(CredentialError):
        store.verify(OLD)
    with pytest.raises(CredentialError):
        PasswordStore(store.path)


def test_file_permissions_links_and_missing_store_fail_closed(store, tmp_path):
    store.path.chmod(0o640)
    with pytest.raises(CredentialError):
        store.verify(OLD)
    store.path.chmod(0o600)
    linked = tmp_path / "linked.json"
    linked.symlink_to(store.path)
    with pytest.raises(CredentialError):
        PasswordStore(linked)
    hardlink = tmp_path / "hardlinked.json"
    os.link(store.path, hardlink)
    with pytest.raises(CredentialError):
        store.verify(OLD)
    hardlink.unlink()
    linked.unlink()
    store.path.unlink()
    with pytest.raises(CredentialError):
        store.verify(OLD)
    with pytest.raises(CredentialError):
        store.replace(NEW)
    assert not store.path.exists()


def test_symlink_parent_and_nonprivate_directory_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(CredentialError):
        PasswordStore.initialize(alias / "credential.json", OLD)
    real.chmod(0o755)
    with pytest.raises(CredentialError):
        PasswordStore.initialize(real / "credential.json", OLD)


def test_failed_replace_preserves_old_credential_without_temp_leaks(store, monkeypatch):
    original = store.path.read_bytes()
    def failure(*_args):
        raise OSError("Synthetic private-path failure")
    monkeypatch.setattr("judge_admin.auth.os.replace", failure)
    with pytest.raises(CredentialError) as caught:
        store.replace(NEW)
    assert not caught.value.committed
    assert "Synthetic" not in str(caught.value)
    assert store.path.read_bytes() == original and store.verify(OLD)
    assert not list(store.path.parent.glob(".password-*"))


def test_rotation_revokes_every_session_and_survives_server_restart(store):
    with running(store) as base:
        first, second = Client(base), Client(base)
        assert first.login() == second.login() == 200
        assert first.call("/api/status")[1]["password_change_enabled"] is True
        code, result, headers = first.change()
        assert code == 200 and result == {"ok": True, "reauthenticate": True}
        assert "Max-Age=0" in headers["Set-Cookie"]
        assert first.call("/api/status")[0] == second.call("/api/status")[0] == 401
        assert first.login(OLD) == 401
        assert first.login(NEW) == 200
        status = first.call("/api/status")[1]
        assert OLD not in json.dumps(status) and NEW not in json.dumps(status)
        assert "digest" not in json.dumps(status)
    with running(PasswordStore(store.path)) as base:
        assert Client(base).login(NEW) == 200
        assert Client(base).login(OLD) == 401


def test_rotation_requires_auth_csrf_schema_and_current_password(store):
    with running(store) as base:
        client = Client(base)
        assert client.change()[0] == 401
        assert client.login() == 200
        assert client.change(overrides={"X-CSRF-Token": "bad"})[0] == 403
        assert client.change(overrides={"Origin": "https://other.example"})[0] == 403
        assert client.change(current="incorrect")[0] == 400
        assert client.call("/api/status")[0] == 200
        assert client.change(confirmation="mismatch")[0] == 400
        assert client.change(new="abc", confirmation="abc")[0] == 400
        assert client.call("/api/password/change", {"password": NEW})[0] == 400
        assert client.call("/api/demo/password/change", {"current_password": OLD,
                           "new_password": NEW, "confirmation": NEW})[0] == 404
        assert store.verify(OLD)


def test_current_password_attempts_are_rate_limited(store):
    with running(store) as base:
        client = Client(base)
        assert client.login() == 200
        for _ in range(10):
            assert client.change(current="incorrect")[0] == 400
        assert client.change()[0] == 429
        assert store.verify(OLD)


def test_precommit_failure_preserves_sessions_and_old_password(store, monkeypatch):
    def failure(*_args):
        raise OSError("Synthetic failure")
    with running(store) as base:
        client = Client(base)
        assert client.login() == 200
        monkeypatch.setattr("judge_admin.auth.os.replace", failure)
        assert client.change()[0] == 503
        assert client.call("/api/status")[0] == 200
        assert client.login(OLD) == 200
        assert client.login(NEW) == 401


def test_postcommit_sync_failure_revokes_sessions_and_reports_reauthentication(store, monkeypatch):
    def failure(*_args):
        raise OSError("Synthetic directory sync failure")
    with running(store) as base:
        first, second = Client(base), Client(base)
        assert first.login() == second.login() == 200
        monkeypatch.setattr("judge_admin.auth._sync_directory", failure)
        code, result, headers = first.change()
        assert code == 503 and result["reauthenticate"]
        assert "Max-Age=0" in headers["Set-Cookie"]
        assert first.call("/api/status")[0] == second.call("/api/status")[0] == 401
        assert store.verify(NEW) and not store.verify(OLD)
        assert first.login(NEW) == 200


def test_concurrent_rotations_cannot_reuse_revoked_session(store):
    with running(store) as base:
        first, second = Client(base), Client(base)
        assert first.login() == second.login() == 200
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda client: client.change()[0], (first, second)))
        assert sorted(results) == [200, 401]
        assert store.verify(NEW)


def test_login_waiting_during_rotation_cannot_create_old_password_session(store, monkeypatch):
    reached, release = threading.Event(), threading.Event()
    original = store.replace

    def blocked_replace(value):
        reached.set()
        assert release.wait(10)
        return original(value)

    with running(store) as base:
        current, queued = Client(base), Client(base)
        assert current.login() == 200
        monkeypatch.setattr(store, "replace", blocked_replace)
        with ThreadPoolExecutor(max_workers=2) as executor:
            rotation = executor.submit(current.change)
            assert reached.wait(10)
            login = executor.submit(queued.login, OLD)
            release.set()
            assert rotation.result(timeout=20)[0] == 200
            assert login.result(timeout=20) == 401
        assert queued.call("/api/status")[0] == 401
        assert queued.login(NEW) == 200


def test_legacy_bootstrap_mode_disables_password_changes():
    storage = SimpleNamespace(status=lambda: {}, backups=lambda: [], audit=lambda: [])
    password = "long-legacy-fixture-administrator-password"
    server = create_server(storage, storage, password, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}")
        assert client.login(password) == 200
        assert client.call("/api/status")[1]["password_change_enabled"] is False
        assert client.change(current=password)[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cli_reads_only_stdin_and_refuses_to_overwrite(tmp_path):
    path = tmp_path / "private" / "credential.json"
    command = [sys.executable, "-m", "judge_admin", "init-password-store", "--file", str(path), "--stdin"]
    result = subprocess.run(command, input=OLD + "\n", text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert OLD not in result.stdout + result.stderr
    assert PasswordStore(path).verify(OLD)
    again = subprocess.run(command, input=NEW + "\n", text=True, capture_output=True)
    assert again.returncode != 0
    assert NEW not in again.stdout + again.stderr
    assert PasswordStore(path).verify(OLD)


@pytest.mark.parametrize("configured", ["missing.json", "corrupt.json"])
def test_configured_store_never_falls_back_to_bootstrap(tmp_path, monkeypatch, configured):
    from judge_admin import __main__ as cli
    from judge_admin import storage
    dirs = {name: str(tmp_path / name) for name in ("live_state", "live_backups", "demo_state", "demo_backups")}
    for directory in dirs.values():
        Path(directory).mkdir(mode=0o700)
    marker = Path(dirs["demo_state"]) / "DEMO_ONLY"
    marker.write_text("demo")
    marker.chmod(0o600)
    bootstrap = tmp_path / "bootstrap"
    bootstrap.write_text("long-fixture-bootstrap-password-unused")
    bootstrap.chmod(0o600)
    if configured == "corrupt.json":
        malformed = tmp_path / configured
        malformed.write_text("invalid")
        malformed.chmod(0o600)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({**dirs, "password_file": str(bootstrap), "password_store": str(tmp_path / configured)}))
    config.chmod(0o600)
    monkeypatch.setattr(storage, "AdminStorage", lambda *_args: object())
    monkeypatch.setattr(sys, "argv", ["judge_admin", "--config", str(config), "serve"])
    with pytest.raises(CredentialError):
        cli.main()
