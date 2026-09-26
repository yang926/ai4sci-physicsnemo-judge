"""Trusted operator workspace enrollment is retryable, atomic and private."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import secrets
import threading

import pytest

from ai4sci_judge.store import Store
from tests.test_judge import answer, completed


@pytest.fixture
def store(tmp_path):
    return Store.initialize(tmp_path / "private", steps=2)


def counts(store):
    with store.connect() as db:
        return tuple(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                     for table in ("participants", "workspace_participants"))


def test_retry_after_delivery_failure_and_restart_preserves_account_and_scores(store):
    key, token = "org-event/instance-1", secrets.token_urlsafe(32)
    person = store.provision_workspace(key, token)
    assert person == store.authenticate(token) == store.workspace_account(key)
    assert person["nickname"] is None
    assert set(person) == {"id", "nickname"}
    assert store.board()["participants"] == []
    store.set_nickname(person["id"], "Learner")
    submission = store.submit(person["id"], "1", {"wave_l1.py": answer("1", "wave_l1.py")})
    job = store.claim()
    store.finish(job, completed(job, 75))
    restarted = Store(store.directory)
    assert restarted.provision_workspace(key, token) == {"id": person["id"], "nickname": "Learner"}
    assert restarted.workspace_account(key) == restarted.authenticate(token)
    assert restarted.history(person["id"])[0]["id"] == submission
    assert restarted.board("1")["participants"][0]["scores"]["1"] == 75
    assert counts(store) == (1, 1)


def test_distinct_workspaces_never_share_an_account(store):
    tokens = [secrets.token_urlsafe(32) for _ in range(2)]
    first = store.provision_workspace("org-a/same-vm", tokens[0])
    second = store.provision_workspace("org-b/same-vm", tokens[1])
    assert first["id"] != second["id"]
    assert store.workspace_account("unknown") is None
    assert counts(store) == (2, 2)


def test_changed_token_cannot_rotate_or_rebind_an_existing_workspace(store):
    original, changed = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    person = store.provision_workspace("org/vm", original)
    with pytest.raises(ValueError, match="already bound"):
        store.provision_workspace("org/vm", changed)
    assert store.authenticate(original) == person
    assert store.authenticate(changed) is None
    assert store.workspace_account("org/vm") == person
    assert counts(store) == (1, 1)


@pytest.mark.parametrize("manual", [False, True])
def test_token_collision_cannot_adopt_another_account_or_leave_an_orphan(store, manual):
    token = store.add_participant("Manual") if manual else secrets.token_urlsafe(32)
    if not manual:
        store.provision_workspace("org/original", token)
    person = store.authenticate(token)
    with pytest.raises(ValueError, match="already assigned"):
        store.provision_workspace("org/impostor", token)
    assert store.workspace_account("org/impostor") is None
    assert store.authenticate(token) == person
    assert counts(store) == (1, 0 if manual else 1)


def test_concurrent_retries_create_exactly_one_binding(store):
    token = secrets.token_urlsafe(32)
    barrier = threading.Barrier(8)

    def provision(_):
        barrier.wait(timeout=10)
        return store.provision_workspace("org/vm", token)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(provision, range(8)))
    assert all(person == results[0] for person in results)
    assert counts(store) == (1, 1)


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_conflicting_bindings_have_only_one_winner(store, same_key):
    token = secrets.token_urlsafe(32)
    barrier = threading.Barrier(2)

    def provision(index):
        key = "org/vm" if same_key else f"org/vm-{index}"
        credential = secrets.token_urlsafe(32) if same_key else token
        barrier.wait(timeout=10)
        try:
            return store.provision_workspace(key, credential)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(provision, range(2)))
    assert sum(person is not None for person in results) == 1
    assert counts(store) == (1, 1)


@pytest.mark.parametrize("key", [None, "", " ", " org/vm", "org/vm ", "x\ny", "x\x00y", "한글", "x" * 257, 1, [], {}, "x';--"])
def test_invalid_keys_cannot_mutate_identity(store, key):
    with pytest.raises(ValueError, match="workspace key"):
        store.provision_workspace(key, secrets.token_urlsafe(32))
    with pytest.raises(ValueError, match="workspace key"):
        store.workspace_account(key)
    assert counts(store) == (0, 0)


@pytest.mark.parametrize("token", [None, "", "a" * 42, "a" * 201, "a" * 42 + " ", "a" * 42 + "\n", "é" * 43, "a" * 42 + "/", 1, [], {}])
def test_invalid_credentials_cannot_mutate_identity(store, token):
    with pytest.raises(ValueError, match="credential"):
        store.provision_workspace("org/vm", token)
    assert counts(store) == (0, 0)


def test_hmac_hex_credentials_and_maximum_bounded_keys_are_supported(store):
    token = secrets.token_hex(32)
    person = store.provision_workspace("x" * 256, token)
    assert store.authenticate(token) == person


def test_storage_and_operator_metadata_never_contain_plaintext_credentials(store):
    token = secrets.token_urlsafe(32)
    person = store.provision_workspace("org/vm", token)
    with store.connect() as db:
        participant = dict(db.execute("SELECT * FROM participants").fetchone())
        assert participant["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in "\n".join(db.iterdump())
    assert set(person) == set(store.workspace_account("org/vm")) == {"id", "nickname"}
    for path in store.directory.glob("judge.sqlite3*"):
        assert token.encode() not in path.read_bytes()


def test_mismatched_frozen_version_rejects_new_and_existing_bindings(store):
    token = secrets.token_urlsafe(32)
    person = store.provision_workspace("org/vm", token)
    with store.connect() as db:
        db.execute("UPDATE settings SET fingerprint='old-version'")
    for key in ("org/vm", "org/new"):
        with pytest.raises(ValueError, match="do not mix scoring versions"):
            store.provision_workspace(key, token)
    assert store.workspace_account("org/vm") == person
    assert counts(store) == (1, 1)
    with store.connect() as db:
        assert db.execute("SELECT fingerprint FROM settings").fetchone()[0] == "old-version"


def test_older_schema_is_not_migrated_even_if_version_is_manually_matched(store):
    with store.connect() as db:
        db.execute("DROP TABLE workspace_participants")
    assert store.workspace_account("org/vm") is None
    with pytest.raises(ValueError, match="do not migrate"):
        store.provision_workspace("org/vm", secrets.token_urlsafe(32))
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE name='workspace_participants'").fetchone() is None
