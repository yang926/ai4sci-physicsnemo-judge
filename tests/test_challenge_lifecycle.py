"""Server-time cutoffs, accepted-job draining and immutable final standings."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
import json
import threading
from types import SimpleNamespace

import pytest

from ai4sci_judge import store as store_module
from ai4sci_judge import web
from ai4sci_judge.catalog import CHALLENGES
from ai4sci_judge.store import ChallengeClosed, LifecycleUpgradeRequired, Store
from tests.test_judge import answer, completed, person


@pytest.fixture
def event(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1000.)
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: clock.now))
    return Store.initialize(tmp_path / "private", steps=2), clock


def sources(challenge="1"):
    filename = CHALLENGES[challenge]["files"][0]
    return {filename: answer(challenge, filename)}


def test_default_open_and_explicit_idempotent_upgrade_preserve_existing_state(event):
    store, _ = event
    participant, _ = person(store)
    identifier = store.submit(participant, "1", sources())
    original_settings, fingerprint = store.settings()
    with store.connect() as db:
        original = dict(db.execute("SELECT * FROM submissions WHERE id=?", (identifier,)).fetchone())
        db.execute("DROP TABLE challenge_lifecycle")
    reopened = Store(store.directory)
    with reopened.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='challenge_lifecycle'").fetchone()
    for operation in (reopened.challenge_status, reopened.board,
                      lambda: reopened.submit(participant, "1", sources())):
        with pytest.raises(LifecycleUpgradeRequired, match="explicit lifecycle upgrade"):
            operation()
    reopened.upgrade_challenge_lifecycle()
    reopened.upgrade_challenge_lifecycle()
    assert reopened.settings() == (original_settings, fingerprint)
    assert set(reopened.challenge_status()["lifecycle"]) == set(CHALLENGES)
    assert all(row["state"] == "open" for row in reopened.challenge_status()["lifecycle"].values())
    with reopened.connect() as db:
        assert dict(db.execute("SELECT * FROM submissions WHERE id=?", (identifier,)).fetchone()) == original
    reopened.transition_challenge("1", "schedule_close")
    reopened.upgrade_challenge_lifecycle()
    assert reopened.challenge_status()["lifecycle"]["1"]["deadline"] == 1300.


def test_countdown_deadline_is_exclusive_and_survives_restart_without_scheduler(event):
    store, clock = event
    before, _ = person(store, "Before cutoff")
    after, _ = person(store, "At cutoff")
    status = store.transition_challenge("1", "schedule_close")
    assert status["server_time"] == 1000.
    assert status["lifecycle"]["1"] == {
        "state": "closing", "deadline": 1300., "finalized_at": None,
        "queued": 0, "running": 0, "system_error": 0,
    }
    with pytest.raises(ValueError, match="cannot be extended"):
        store.transition_challenge("1", "schedule_close")
    clock.now = 1299.999
    accepted = store.submit(before, "1", sources())
    restarted = Store(store.directory)
    clock.now = 1300.
    # Check the submission boundary before any status read can advance state.
    with pytest.raises(ChallengeClosed):
        restarted.submit(after, "1", sources())
    with pytest.raises(ChallengeClosed):
        restarted.submit(before, "1", sources())  # Duplicate uploads also reject.
    assert restarted.history(after) == []
    state = restarted.challenge_status()
    assert state["lifecycle"]["1"]["state"] == "closed"
    assert state["lifecycle"]["1"]["queued"] == 1
    assert state["lifecycle"]["2"]["state"] == "open"
    assert restarted.submit(after, "2", sources("2"))
    job = restarted.claim()
    assert job["id"] == accepted
    restarted.finish(job, completed(job, 90))
    final = restarted.transition_challenge("1", "finalize")
    assert final["lifecycle"]["1"]["state"] == "finalized"
    assert final["lifecycle"]["2"]["queued"] == 1


def test_submit_uses_clock_after_acquiring_real_sqlite_write_lock(event, monkeypatch):
    store, clock = event
    participant, _ = person(store)
    store.transition_challenge("1", "schedule_close")
    clock.now = 1299.
    attempting_lock = threading.Event()
    original_connect = store.connect

    @contextmanager
    def watched_connect():
        with original_connect() as db:
            db.set_trace_callback(lambda sql: attempting_lock.set() if sql == "BEGIN IMMEDIATE" else None)
            yield db

    with ThreadPoolExecutor(max_workers=1) as pool:
        with original_connect() as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            monkeypatch.setattr(store, "connect", watched_connect)
            future = pool.submit(store.submit, participant, "1", sources())
            assert attempting_lock.wait(5), "Submission did not attempt SQLite's write lock"
            clock.now = 1300.
        with pytest.raises(ChallengeClosed):
            future.result(timeout=5)
    assert store.history(participant) == []


def test_deadline_status_and_board_are_read_only_even_while_an_operator_holds_write_lock(event, monkeypatch):
    store, clock = event
    store.transition_challenge("1", "schedule_close")
    clock.now = 1300.
    original_connect = store.connect

    @contextmanager
    def read_only_connect():
        with original_connect() as db:
            db.execute("PRAGMA query_only=ON")
            yield db
            assert db.total_changes == 0

    with original_connect() as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(store, "connect", read_only_connect)
        assert store.challenge_status()["lifecycle"]["1"]["state"] == "closed"
        assert store.board("1")["lifecycle"]["1"]["state"] == "closed"
        assert blocker.execute("SELECT state FROM challenge_lifecycle WHERE challenge='1'").fetchone()[0] == "closing"


def test_close_and_submit_race_cannot_accept_after_close_wins_lock(event, monkeypatch):
    store, clock = event
    participant, _ = person(store)
    attempting_lock = threading.Event()
    original_connect = store.connect

    @contextmanager
    def watched_connect():
        with original_connect() as db:
            db.set_trace_callback(lambda sql: attempting_lock.set() if sql == "BEGIN IMMEDIATE" else None)
            yield db

    with ThreadPoolExecutor(max_workers=1) as pool:
        with original_connect() as closing:
            closing.execute("BEGIN IMMEDIATE")
            monkeypatch.setattr(store, "connect", watched_connect)
            future = pool.submit(store.submit, participant, "1", sources())
            assert attempting_lock.wait(5)
            closing.execute("UPDATE challenge_lifecycle SET state='closed',deadline=? WHERE challenge='1'", (clock.now,))
        with pytest.raises(ChallengeClosed):
            future.result(timeout=5)
    assert store.history(participant) == []


def test_finalization_waits_for_accepted_work_and_freezes_names_scores_and_membership(event):
    store, clock = event
    participant, _ = person(store, "Name at finalization")
    with pytest.raises(ValueError, match="Close"):
        store.transition_challenge("1", "finalize")
    store.submit(participant, "1", sources())
    clock.now += 1
    store.transition_challenge("1", "close")
    with pytest.raises(ValueError, match="queued or running"):
        store.transition_challenge("1", "finalize")
    job = store.claim()
    with pytest.raises(ValueError, match="queued or running"):
        store.transition_challenge("1", "finalize")
    store.finish(job, completed(job, 75))
    clock.now += 1
    store.transition_challenge("1", "finalize")
    frozen = store.board("1")
    assert frozen["server_time"] == 1002.
    assert set(frozen["lifecycle"]) == {"1"}
    assert frozen["lifecycle"]["1"]["finalized_at"] == 1002.
    assert frozen["participants"] == [{"nickname": "Name at finalization", "scores": {"1": 75}, "challenge_ranks": {"1": 1}}]
    assert participant not in json.dumps(frozen)
    store.set_nickname(participant, "Later nickname")
    person(store, "Later participant")
    # Defensive guarantee: even an operator's subsequent live-row change cannot
    # silently change the finalized display or the overall score contribution.
    with store.connect() as db:
        db.execute("UPDATE submissions SET score=100 WHERE id=?", (job["id"],))
    assert Store(store.directory).board("1")["participants"] == frozen["participants"]
    overall = {row["nickname"]: row for row in store.board()["participants"]}
    assert overall["Later nickname"]["scores"]["1"] == 75
    assert overall["Later nickname"]["total"] == 75
    assert overall["Later participant"]["scores"]["1"] is None
    for action in ("open", "start", "schedule_close", "close", "finalize", "retry_errors"):
        with pytest.raises(ValueError):
            store.transition_challenge("1", action)
    with pytest.raises(ChallengeClosed):
        store.submit(participant, "1", sources())


def test_system_errors_block_finalization_but_can_retry_original_accepted_work(event):
    store, clock = event
    participant, _ = person(store)
    submission = store.submit(participant, "1", sources())
    job = store.claim()
    store.finish(job, status="system_error", error="Infrastructure unavailable")
    clock.now += 1
    store.transition_challenge("1", "close")
    with pytest.raises(ValueError, match="Infrastructure errors"):
        store.transition_challenge("1", "finalize")
    with store.connect() as db:
        original = dict(db.execute("SELECT * FROM submissions WHERE id=?", (submission,)).fetchone())
    status = store.transition_challenge("1", "retry_errors")
    assert status["retried"] == 1
    assert status["lifecycle"]["1"]["queued"] == 1
    assert status["lifecycle"]["1"]["system_error"] == 0
    with store.connect() as db:
        retried = dict(db.execute("SELECT * FROM submissions WHERE id=?", (submission,)).fetchone())
    for key in ("id", "participant", "challenge", "created", "sources", "source_hash", "fingerprint"):
        assert retried[key] == original[key]
    for key in ("started", "finished", "lease", "score", "result", "error"):
        assert retried[key] is None
    with pytest.raises(ChallengeClosed):
        store.submit(participant, "1", sources())
    store.finish(job, completed(job, 100))  # Old worker lease cannot finish a retry.
    assert store.history(participant)[0]["status"] == "queued"
    retry = store.claim()
    assert retry["id"] == submission and retry["lease"] != job["lease"]
    store.finish(retry, completed(retry, 90))
    store.transition_challenge("1", "finalize")
    assert store.board("1")["participants"][0]["scores"]["1"] == 90


def test_final_snapshot_defensively_ignores_post_cutoff_completed_rows(event):
    store, clock = event
    participant, _ = person(store)
    store.submit(participant, "1", sources())
    job = store.claim()
    store.finish(job, completed(job, 100))
    clock.now += 1
    store.transition_challenge("1", "close")
    with store.connect() as db:
        db.execute("UPDATE submissions SET created=? WHERE id=?", (clock.now, job["id"]))
    store.transition_challenge("1", "finalize")
    assert store.board("1")["participants"][0]["scores"]["1"] is None
    assert store.board()["participants"][0]["total"] == 0


@pytest.mark.parametrize("terminal", ["time_limit", "completed"])
def test_learner_terminal_results_do_not_block_or_retry(event, terminal):
    store, clock = event
    participant, _ = person(store)
    store.submit(participant, "1", sources())
    job = store.claim()
    result = completed(job, 0) if terminal == "completed" else None
    store.finish(job, status=terminal, result=result)
    clock.now += 1
    store.transition_challenge("1", "close")
    assert store.transition_challenge("1", "retry_errors")["retried"] == 0
    assert store.transition_challenge("1", "finalize")["lifecycle"]["1"]["state"] == "finalized"


def test_retry_filters_cutoff_and_challenge_and_never_reopens(event):
    store, clock = event
    first, _ = person(store, "First")
    other, _ = person(store, "Other")
    for participant, challenge in ((first, "1"), (other, "2")):
        store.submit(participant, challenge, sources(challenge))
        store.finish(store.claim(), status="system_error", error="test")
    with pytest.raises(ValueError, match="closing or closed"):
        store.transition_challenge("1", "retry_errors")
    store.transition_challenge("1", "schedule_close")
    assert store.transition_challenge("1", "retry_errors")["retried"] == 1
    store.finish(store.claim(), status="system_error", error="test")
    clock.now = 1300.
    with store.connect() as db:
        db.execute("UPDATE submissions SET created=1300 WHERE participant=?", (first,))
    assert store.transition_challenge("1", "retry_errors")["retried"] == 0
    status = store.challenge_status()["lifecycle"]
    assert status["1"]["state"] == "closed"
    assert status["1"]["system_error"] == status["2"]["system_error"] == 1


def test_http_rejects_closed_submission_with_actionable_conflict_without_socket(event, monkeypatch):
    store, _ = event
    participant, token = person(store)
    store.transition_challenge("1", "close")
    # Instantiate the actual handler without binding a port or starting a server.
    monkeypatch.setattr(web, "ThreadingHTTPServer", lambda address, handler: handler)
    handler_type = web.create_server(store)
    handler = handler_type.__new__(handler_type)
    body = json.dumps({"challenge": "1", "sources": sources()}).encode()
    handler.path = "/api/submissions"
    handler.headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Content-Length": str(len(body))}
    handler.rfile = BytesIO(body)
    handler.check_host = lambda: True
    responses = []
    handler.send = lambda status, payload: responses.append((status, payload))
    handler.do_POST()
    assert responses[0][0] == 409
    assert responses[0][1]["error_code"] == "challenge_closed"
    assert "마감" in responses[0][1]["error"]
    assert "closed" in responses[0][1]["error"]
    assert store.history(participant) == []
