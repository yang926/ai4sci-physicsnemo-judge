"""Private, durable administrator credentials, separate from scoring state.

Short user-chosen passwords are supported for the inner login behind the
owner-only Brev Secure Link. This is not a substitute for that outer boundary.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile


ITERATIONS = 600_000
MAX_FILE_BYTES = 4096


class CredentialError(ValueError):
    """A credential operation failed without exposing credential material."""

    def __init__(self, message="Administrator credential storage is unavailable.", *, committed=False):
        super().__init__(message)
        self.committed = committed


def validate_password(value):
    if not isinstance(value, str) or not 4 <= len(value) <= 128 or not value.isprintable():
        raise ValueError("비밀번호는 공백을 포함하여 4~128자의 표시 가능한 문자로 입력하세요.")
    return value


def _private_path(value, *, directory=False):
    path = Path(os.path.abspath(os.fspath(value)))
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise CredentialError()
    if path.exists():
        info = path.stat()
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if (not expected(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or (not directory and info.st_nlink != 1)):
            raise CredentialError()
    return path


def _record(password):
    validate_password(password)
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return {"version": 1, "algorithm": "pbkdf2-sha256", "iterations": ITERATIONS,
            "salt": salt.hex(), "digest": digest.hex()}


def _encode(record):
    return (json.dumps(record, sort_keys=True) + "\n").encode("ascii")


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class PasswordStore:
    """A single service-owned 0600 verifier file; never stores plaintext.

    Initialize explicitly, then pass the returned instance as ``credential_store``
    to ``create_server``. Existing malformed/missing files always fail closed.
    ``replace`` must be serialized with authentication by the calling server.
    """

    def __init__(self, path):
        self.path = _private_path(path)
        _private_path(self.path.parent, directory=True)
        self._read()

    @classmethod
    def initialize(cls, path, password):
        data = _encode(_record(password))
        path = _private_path(path)
        _private_path(path.parent, directory=True)
        if not path.parent.is_dir():
            raise CredentialError()
        # O_EXCL deliberately refuses to reset an existing administrator password.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(path.parent)
        except OSError:
            raise CredentialError() from None
        return cls(path)

    def _read(self):
        try:
            _private_path(self.path)
            _private_path(self.path.parent, directory=True)
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1):
                    raise CredentialError()
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise CredentialError()
            value = json.loads(data)
            if (not isinstance(value, dict)
                    or set(value) != {"version", "algorithm", "iterations", "salt", "digest"}
                    or type(value["version"]) is not int or value["version"] != 1
                    or value["algorithm"] != "pbkdf2-sha256"
                    or type(value["iterations"]) is not int or value["iterations"] != ITERATIONS
                    or any(not isinstance(value[key], str) or len(value[key]) != 64
                           or any(c not in "0123456789abcdef" for c in value[key])
                           for key in ("salt", "digest"))):
                raise CredentialError()
            return value
        except (OSError, ValueError, TypeError, UnicodeError):
            raise CredentialError() from None

    def verify(self, candidate):
        record = self._read()
        if not isinstance(candidate, str) or not 4 <= len(candidate) <= 128 or not candidate.isprintable():
            return False
        actual = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"),
                                     bytes.fromhex(record["salt"]), record["iterations"])
        return hmac.compare_digest(actual, bytes.fromhex(record["digest"]))

    def replace(self, password):
        data = _encode(_record(password))
        self._read()  # Never recreate or replace a tampered credential file.
        temporary = None
        committed = False
        try:
            fd, temporary = tempfile.mkstemp(prefix=".password-", dir=self.path.parent)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._read()
            os.replace(temporary, self.path)
            committed = True
            _sync_directory(self.path.parent)
        except (OSError, CredentialError):
            raise CredentialError(committed=committed) from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
