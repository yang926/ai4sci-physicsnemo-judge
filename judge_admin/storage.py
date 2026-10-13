"""Private SQLite operations, independent from the frozen scoring package.

Backups contain participant credentials (hashed) and submitted source code.
Only an authenticated operator may download them. No general file-path or
uploaded-database restore API is provided.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import uuid

from ai4sci_judge.catalog import CHALLENGES, PROJECT_ROOT, ROOT
from ai4sci_judge.store import Store


class AdminError(ValueError):
    """A requested operator action is invalid or unsafe."""


BACKUP_ID = re.compile(r"[0-9]{8}T[0-9]{12}Z-[a-f0-9]{12}\Z")
TABLES = ("settings", "participants", "revoked_credentials", "workspace_participants", "submissions", "challenge_lifecycle")
CHALLENGE_ACTIONS = frozenset({"schedule_close", "close", "retry_errors", "finalize"})


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_path(value, *, directory=False, private=True):
    """Reject symlinks before resolution, including any ancestor component."""
    path = Path(os.path.abspath(os.fspath(value)))
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise AdminError("Symlinks are not allowed for private database or backup paths.")
    if path.exists():
        info = path.stat()
        if directory and not stat.S_ISDIR(info.st_mode):
            raise AdminError("Expected a private directory.")
        if not directory and not stat.S_ISREG(info.st_mode):
            raise AdminError("Expected a regular private file.")
        if not directory and info.st_nlink != 1:
            raise AdminError("Hard-linked private database and backup files are not allowed.")
        if private and (info.st_mode & 0o077 or info.st_uid != os.geteuid()):
            raise AdminError("Database and backup storage must be owner-only and owned by the service user.")
    return path


def _exclusive_file(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class AdminStorage:
    def __init__(self, state_directory, backup_directory, controller):
        self.state = _safe_path(state_directory, directory=True)
        self.path = _safe_path(self.state / "judge.sqlite3")
        if not self.path.is_file():
            raise AdminError("The existing judge database was not found.")
        self.backup_root = _safe_path(backup_directory, directory=True)
        for forbidden in (PROJECT_ROOT, ROOT, self.state):
            if self.backup_root.is_relative_to(forbidden):
                raise AdminError("Keep backups outside the repositories and the active state directory.")
        if self.state.is_relative_to(self.backup_root):
            raise AdminError("The active state must not be nested inside its backup directory.")
        self.backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _safe_path(self.backup_root, directory=True)
        self.controller = controller
        self.state_id = hashlib.sha256(str(self.state).encode()).hexdigest()
        self._thread_lock = threading.RLock()
        self._backup_cache = {}
        self._store = Store(self.state)
        self.store = self._store
        self._store.require_current_version()
        with self._connect(readonly=True) as db:
            self._schema = self._schema_hash(db)
            self._settings, self._fingerprint = self._configuration(db)

    @contextmanager
    def _lock(self):
        with self._thread_lock:
            path = self.backup_root / ".operator.lock"
            _safe_path(path)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @contextmanager
    def _connect(self, path=None, readonly=False):
        path = _safe_path(path or self.path)
        uri = path.as_uri() + ("?mode=ro" if readonly else "?mode=rw")
        db = sqlite3.connect(uri, uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _schema_hash(db):
        rows = [tuple(row) for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )]
        return hashlib.sha256(_json_bytes(rows)).hexdigest()

    @staticmethod
    def _configuration(db):
        rows = db.execute("SELECT value,fingerprint FROM settings").fetchall()
        if len(rows) != 1:
            raise AdminError("Invalid judge settings table.")
        return json.loads(rows[0][0]), rows[0][1]

    @staticmethod
    def _counts(db):
        grouped = {row[0]: row[1] for row in db.execute("SELECT status,COUNT(*) FROM submissions GROUP BY status")}
        return {
            "participants": db.execute("SELECT COUNT(*) FROM participants").fetchone()[0],
            "named_participants": db.execute("SELECT COUNT(*) FROM participants WHERE nickname IS NOT NULL").fetchone()[0],
            "bindings": db.execute("SELECT COUNT(*) FROM workspace_participants").fetchone()[0],
            "revoked": db.execute("SELECT COUNT(*) FROM revoked_credentials").fetchone()[0],
            "submissions": sum(grouped.values()), "queued": grouped.get("queued", 0),
            "running": grouped.get("running", 0), "completed": grouped.get("completed", 0),
            "submission_statuses": grouped,
        }

    def _validate(self, db):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='challenge_lifecycle'").fetchone():
            raise AdminError("Backup predates Challenge lifecycle support and cannot restore closure or final results.")
        if self._schema_hash(db) != self._schema:
            raise AdminError("Backup schema does not match this judge.")
        if {row[0] for row in db.execute("SELECT challenge FROM challenge_lifecycle")} != set(CHALLENGES):
            raise AdminError("Backup Challenge lifecycle records are incomplete or invalid.")
        settings, revision = self._configuration(db)
        if settings != self._settings or revision != self._fingerprint:
            raise AdminError("Backup scoring settings or fingerprint do not match this event.")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AdminError("Database integrity check failed.")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AdminError("Database contains invalid participant references.")
        if db.execute("SELECT 1 FROM submissions s LEFT JOIN participants p ON p.id=s.participant WHERE p.id IS NULL LIMIT 1").fetchone():
            raise AdminError("Database contains orphaned submissions.")

    def status(self):
        with self._connect(readonly=True) as db:
            db.execute("BEGIN")
            counts = self._counts(db)
            _, revision = self._configuration(db)
        control = self.controller.status()
        return {**counts, **self._store.challenge_status(), "fingerprint": revision, "state": self.state.name,
                "state_id": self.state_id, "maintenance": control["intake_paused"],
                "controller": control,
                "ready_to_reset": bool(control["intake_paused"] and not (counts["queued"] or counts["running"]))}

    def _audit(self, action, **details):
        path = self.backup_root / "audit.jsonl"
        _safe_path(path)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "ab") as stream:
            stream.write(_json_bytes({"at": datetime.now(timezone.utc).isoformat(), "action": action, **details}))
            stream.flush()
            os.fsync(stream.fileno())

    def audit(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise AdminError("Choose an audit limit from 1 to 200.")
        with self._lock():
            path = _safe_path(self.backup_root / "audit.jsonl")
            if not path.exists():
                return []
            from collections import deque
            with path.open(encoding="utf-8") as stream:
                return [json.loads(line) for line in deque(stream, maxlen=limit)][::-1]

    def _backup_path(self, identifier):
        if not isinstance(identifier, str) or BACKUP_ID.fullmatch(identifier) is None:
            raise AdminError("Unknown backup identifier.")
        folder = _safe_path(self.backup_root / identifier, directory=True)
        if not folder.is_dir():
            raise AdminError("Unknown backup identifier.")
        return folder

    def _manifest(self, identifier, verify=False):
        folder = self._backup_path(identifier)
        manifest_path = _safe_path(folder / "manifest.json")
        database = _safe_path(folder / "judge.sqlite3")
        try:
            if manifest_path.stat().st_size > 32768:
                raise AdminError("Invalid backup manifest.")
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError) as exc:
            raise AdminError("Invalid backup manifest.") from exc
        if (not isinstance(manifest, dict)
                or manifest.get("id") != identifier or manifest.get("state_id") != self.state_id
                or manifest.get("format") != 1 or not database.is_file()):
            raise AdminError("Backup does not belong to this state.")
        if database.stat().st_size != manifest.get("bytes"):
            raise AdminError("Backup size verification failed.")
        if verify and _digest(database) != manifest.get("sha256"):
            raise AdminError("Backup hash verification failed.")
        # Return an explicit safe allowlist, not arbitrary private manifest keys.
        safe = {key: manifest[key] for key in (
            "id", "format", "state_id", "created_at", "reason", "bytes", "sha256",
            "fingerprint", "schema_hash", "settings_hash", "counts", "restorable")}
        safe["compatible"] = False
        if verify:
            try:
                with self._connect(database, readonly=True) as snapshot:
                    self._validate(snapshot)
                    counts = self._counts(snapshot)
                safe["compatible"] = bool(manifest["restorable"] and not (counts["queued"] or counts["running"]))
            except (AdminError, sqlite3.Error):
                pass
        return safe

    def backups(self):
        with self._lock():
            result = []
            for folder in sorted(self.backup_root.iterdir(), reverse=True):
                if BACKUP_ID.fullmatch(folder.name):
                    try:
                        # Immutable private snapshots can be numerous. Recheck
                        # changed files only for list polling; downloads and
                        # restores always verify the complete bytes again.
                        _safe_path(folder, directory=True)
                        signatures = []
                        for filename in ("judge.sqlite3", "manifest.json"):
                            info = _safe_path(folder / filename).stat()
                            signatures.append((info.st_dev, info.st_ino, info.st_size,
                                               info.st_mtime_ns, info.st_ctime_ns, info.st_mode))
                        signature = tuple(signatures)
                        cached = self._backup_cache.get(folder.name)
                        if cached is None or cached[0] != signature:
                            cached = (signature, self._manifest(folder.name, verify=True))
                            self._backup_cache[folder.name] = cached
                        result.append(dict(cached[1]))
                    except (AdminError, KeyError, OSError, sqlite3.Error):
                        continue
            return result

    def backup_file(self, identifier):
        with self._lock():
            self._manifest(identifier, verify=True)
            return self._backup_path(identifier) / "judge.sqlite3"

    def _create_backup(self, reason):
        now = datetime.now(timezone.utc)
        identifier = now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:12]
        pending = self.backup_root / (".pending-" + identifier)
        pending.mkdir(mode=0o700)
        target = pending / "judge.sqlite3"
        _exclusive_file(target, b"")
        with self._connect(readonly=True) as source, sqlite3.connect(target) as destination:
            # SQLite's backup API includes a consistent committed WAL snapshot.
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
            destination.commit()
        with self._connect(target, readonly=True) as snapshot:
            self._validate(snapshot)
            counts = self._counts(snapshot)
        manifest = {
            "id": identifier, "format": 1, "state_id": self.state_id,
            "created_at": now.isoformat(), "reason": reason, "bytes": target.stat().st_size,
            "sha256": _digest(target), "fingerprint": self._fingerprint,
            "schema_hash": self._schema, "settings_hash": hashlib.sha256(_json_bytes(self._settings)).hexdigest(),
            "counts": counts, "restorable": not (counts["queued"] or counts["running"]),
        }
        _exclusive_file(pending / "manifest.json", _json_bytes(manifest))
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        target.chmod(0o400)
        (pending / "manifest.json").chmod(0o400)
        _sync_directory(pending)
        pending.chmod(0o500)
        pending.rename(self.backup_root / identifier)
        _sync_directory(self.backup_root)
        self._audit("backup", backup_id=identifier, reason=reason, counts=counts)
        return {**manifest, "compatible": manifest["restorable"]}

    def create_backup(self, reason="manual"):
        if reason not in {"manual", "scheduled"}:
            raise AdminError("Invalid backup reason.")
        with self._lock():
            self._store.require_current_version()
            return self._create_backup(reason)

    def pause(self):
        with self._lock():
            self._audit("pause_requested")
            control = self.controller.pause()
            if not control["intake_paused"]:
                raise AdminError("Submission intake could not be paused.")
            self._audit("paused")
            return self.status()

    def resume(self):
        with self._lock():
            self._store.require_current_version()
            self._audit("resume_requested")
            control = self.controller.resume()
            if control["intake_paused"] or control["workers_stopped"]:
                raise AdminError("Judge services did not resume successfully.")
            self._audit("resumed")
            return self.status()

    def challenge_action(self, challenge, action):
        if not isinstance(challenge, str) or challenge not in CHALLENGES:
            raise AdminError("Choose Challenge 1, 2, 3 or 4.")
        if not isinstance(action, str) or action not in CHALLENGE_ACTIONS:
            raise AdminError("Choose schedule_close, close, retry_errors or finalize.")
        with self._lock():
            self._store.require_current_version()
            self._audit("challenge_action_requested", challenge=challenge, transition=action)
            result = self._store.transition_challenge(challenge, action)
            self._audit("challenge_action_completed", challenge=challenge, transition=action,
                        lifecycle=result["lifecycle"][challenge],
                        **({"retried": result["retried"]} if "retried" in result else {}))
            return result

    def _quiesce(self):
        if not self.controller.status()["intake_paused"]:
            raise AdminError("Pause submission intake before resetting or restoring.")
        with self._connect(readonly=True) as db:
            counts = self._counts(db)
        if counts["queued"] or counts["running"]:
            raise AdminError("Wait for all queued and running submissions to finish.")
        control = self.controller.quiesce()
        if not control["intake_paused"] or not control["workers_stopped"]:
            raise AdminError("All judge writers must be stopped before this operation.")

    def _transaction_ready(self, db):
        db.execute("BEGIN IMMEDIATE")
        self._validate(db)
        counts = self._counts(db)
        if counts["queued"] or counts["running"]:
            raise AdminError("New work appeared; reset or restore was refused.")
        control = self.controller.status()
        if not control["intake_paused"] or not control["workers_stopped"]:
            raise AdminError("Judge writers resumed; reset or restore was refused.")

    def reset(self, scope, confirmation):
        expected = {"scores": "RESET SCORES", "event": "RESET EVENT"}
        if scope not in expected or confirmation != expected[scope]:
            raise AdminError("Enter the exact reset confirmation phrase.")
        with self._lock():
            self._store.require_current_version()
            self._quiesce()
            with self._connect() as db:
                try:
                    self._transaction_ready(db)
                    backup = self._create_backup("before_reset_" + scope)
                    self._audit("reset_requested", scope=scope, backup_id=backup["id"])
                    db.execute("DELETE FROM submissions")
                    db.execute("UPDATE challenge_lifecycle SET state='open',deadline=NULL,finalized_at=NULL,snapshot=NULL")
                    if scope == "event":
                        db.execute("DELETE FROM workspace_participants")
                        db.execute("DELETE FROM revoked_credentials")
                        db.execute("DELETE FROM participants")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            self._audit("reset_completed", scope=scope, backup_id=backup["id"])
            return {"scope": scope, "backup": backup, "status": self.status(),
                    "warning": ("External enrollment master is not rotated. Previously issued VM credentials may be re-enrolled after resume."
                                if scope == "event" else "Participant registrations, bindings and revocations were preserved.")}

    def restore(self, identifier, confirmation):
        if confirmation != "RESTORE BACKUP":
            raise AdminError("Enter the exact restore confirmation phrase.")
        with self._lock():
            self._store.require_current_version()
            manifest = self._manifest(identifier, verify=True)
            snapshot_path = self._backup_path(identifier) / "judge.sqlite3"
            with self._connect(snapshot_path, readonly=True) as snapshot:
                self._validate(snapshot)
                counts = self._counts(snapshot)
                if counts["queued"] or counts["running"] or not manifest["restorable"]:
                    raise AdminError("Cannot restore a snapshot containing queued or running jobs.")
            self._quiesce()
            with self._connect() as db:
                try:
                    self._transaction_ready(db)
                    backup = self._create_backup("before_restore")
                    self._audit("restore_requested", source_backup_id=identifier, backup_id=backup["id"])
                    db.execute("ATTACH DATABASE ? AS snapshot", (snapshot_path.as_uri() + "?mode=ro",))
                    # Revalidate the attached file before reading its data.
                    if self._schema_hash(db) != self._schema:
                        raise AdminError("Active database schema changed.")
                    for table in reversed(TABLES):
                        db.execute(f"DELETE FROM main.{table}")
                    for table in TABLES:
                        db.execute(f"INSERT INTO main.{table} SELECT * FROM snapshot.{table}")
                    self._validate(db)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            self._audit("restore_completed", source_backup_id=identifier, backup_id=backup["id"])
            return {"restored": identifier, "backup": backup, "status": self.status(),
                    "warning": "Restoring rolls participant registrations and revocations back to the snapshot. External enrollment master is not restored."}
