#!/usr/bin/env python3
"""Exercise administrative recovery using ONLY the isolated demo HTTP API.

The live event is read-only throughout. The only non-demo POSTs are login and
logout; an explicit allowlist below enforces this even if the script changes.
Passwords, cookies, CSRF tokens and downloaded database contents are never
printed or written to disk.
"""
import argparse
import hashlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import stat
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


TIMEOUT = 210
INVARIANTS = ("participants", "named_participants", "bindings", "revoked", "submissions", "fingerprint")
ALLOWED_POSTS = frozenset({
    "/api/login", "/api/logout", "/api/demo/pause", "/api/demo/resume",
    "/api/demo/backup", "/api/demo/reset", "/api/demo/restore",
})


class RehearsalError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise RehearsalError(message)


def report(step, **details):
    print(json.dumps({"step": step, **details}, sort_keys=True), flush=True)


def counts(status):
    return {key: status[key] for key in INVARIANTS}


def stable_board(payload):
    """Compare event content, not the new response-time clock sample."""
    return {key: value for key, value in payload.items() if key != "server_time"}


def private_password(path):
    path = Path(path)
    require(not path.is_symlink(), "Password file must not be a symbolic link.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and not (info.st_mode & 0o077),
                "Password file must be a private regular file.")
        require(24 <= info.st_size <= 4096, "Unexpected password file size.")
        value = stream.read(4096).strip()
    require(len(value) >= 24, "Unexpected password length.")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RehearsalError("HTTP redirects are not permitted in operator rehearsal.")


class Client:
    def __init__(self, url):
        parsed = urlsplit(url)
        require(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
                and parsed.username is None and parsed.password is None
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
                "Use a loopback HTTP origin, for example http://127.0.0.1:8092.")
        require(parsed.port is not None and 1 <= parsed.port <= 65535, "Specify the exact administrator port.")
        self.origin = "http://" + parsed.netloc
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()), NoRedirect())
        self.csrf = None
        self.demo_state_id = None

    def request(self, path, payload=None):
        require(path.startswith("/") and not path.startswith("//"), "Invalid API path.")
        headers = {"Accept": "application/json", "Origin": self.origin}
        body = None
        if payload is not None:
            require(path in ALLOWED_POSTS, "Refused an attempted non-demo database mutation.")
            headers["Content-Type"] = "application/json"
            if self.csrf:
                headers["X-CSRF-Token"] = self.csrf
            body = json.dumps(payload).encode()
        request = Request(self.origin + path, data=body, headers=headers)
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                value = json.load(response)
        except HTTPError as exc:
            raise RehearsalError(f"HTTP {exc.code} at the requested administrative operation.") from None
        except (URLError, TimeoutError, ValueError):
            raise RehearsalError("Administrative API request failed or returned invalid JSON.") from None
        require(isinstance(value, dict), "Administrative API returned an unexpected response.")
        return value

    def demo_status(self):
        status = self.request("/api/demo/status")
        require(status.get("mode") == "demo", "Refused an endpoint not explicitly marked as demo.")
        require(isinstance(status.get("state_id"), str) and len(status["state_id"]) == 64,
                "Demo state identity was missing.")
        if self.demo_state_id is not None:
            require(status["state_id"] == self.demo_state_id, "Demo state changed during rehearsal.")
        return status

    def demo_post(self, action, payload=None):
        self.demo_status()  # Check isolation before every mutation, not only at startup.
        response = self.request("/api/demo/" + action, {} if payload is None else payload)
        require(response.get("ok") is True and isinstance(response.get("result"), dict),
                "Demo operation did not report success.")
        return response["result"]

    def check_download(self, backup):
        identifier = backup.get("id")
        require(isinstance(identifier, str) and re.fullmatch(r"[0-9]{8}T[0-9]{12}Z-[a-f0-9]{12}", identifier),
                "Unexpected demo backup identifier.")
        path = "/api/demo/backups/" + quote(identifier, safe="")
        request = Request(self.origin + path, headers={"Origin": self.origin})
        digest, size = hashlib.sha256(), 0
        try:
            with self.opener.open(request, timeout=TIMEOUT) as response:
                header = response.read(16)
                require(header == b"SQLite format 3\x00", "Downloaded backup is not an SQLite database.")
                digest.update(header)
                size += len(header)
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
        except (HTTPError, URLError, TimeoutError):
            raise RehearsalError("Demo backup download failed.") from None
        require(size == backup.get("bytes") and digest.hexdigest() == backup.get("sha256"),
                "Downloaded demo backup did not match the recorded size and hash.")
        return size


def run(client, password):
    login = client.request("/api/login", {"password": password})
    client.csrf = login.get("csrf_token")
    require(isinstance(client.csrf, str) and len(client.csrf) >= 32, "Login did not return an administrative session.")
    demo_started = False
    try:
        live_before = client.request("/api/status")
        require(live_before.get("mode") == "live", "Live status endpoint was not explicitly identified.")
        baseline_status = client.demo_status()
        require(baseline_status["state_id"] != live_before.get("state_id"), "Live and demo states are not isolated.")
        client.demo_state_id = baseline_status["state_id"]
        require(baseline_status["participants"] == 30 and baseline_status["submissions"] > 0,
                "This rehearsal requires the original 30-person synthetic demo fixture.")
        require(baseline_status["queued"] == baseline_status["running"] == 0,
                "Demo must have no queued or running jobs.")
        original_board = client.request("/demo/api/board")
        require(len(original_board.get("participants", [])) == 30, "Demo board did not contain the expected participants.")
        report("preflight", passed=True, live=counts(live_before), demo=counts(baseline_status), synthetic=True)

        demo_started = True
        client.demo_post("pause")
        baseline_backup = client.demo_post("backup")
        require(baseline_backup.get("compatible") is True, "Demo baseline backup is not restorable.")
        downloaded = client.check_download(baseline_backup)
        baseline_id = baseline_backup["id"]
        report("demo_backup_download", passed=True, bytes=downloaded, backup_id=baseline_id)

        client.demo_post("reset", {"scope": "scores", "confirmation": "RESET SCORES"})
        cleared = client.demo_status()
        require(cleared["participants"] == 30 and cleared["submissions"] == 0 and cleared["maintenance"],
                "Score reset did not preserve demo participants while clearing submissions.")
        require(cleared["named_participants"] == baseline_status["named_participants"]
                and cleared["bindings"] == baseline_status["bindings"] and cleared["revoked"] == baseline_status["revoked"],
                "Score reset changed demo identity metadata.")
        report("demo_score_reset", passed=True, counts=counts(cleared))

        client.demo_post("restore", {"backup_id": baseline_id, "confirmation": "RESTORE BACKUP"})
        require(stable_board(client.request("/demo/api/board")) == stable_board(original_board), "Demo scores/ranks differed after score-reset recovery.")
        report("demo_score_restore", passed=True)

        client.demo_post("reset", {"scope": "event", "confirmation": "RESET EVENT"})
        empty = client.demo_status()
        require(all(empty[key] == 0 for key in INVARIANTS if key != "fingerprint") and empty["maintenance"],
                "Full reset did not clear the demo event while remaining paused.")
        require(client.request("/demo/api/board")["participants"] == [], "Full reset left rows on the demo board.")
        report("demo_full_reset", passed=True, counts=counts(empty))

        client.demo_post("restore", {"backup_id": baseline_id, "confirmation": "RESTORE BACKUP"})
        restored = client.demo_status()
        require(counts(restored) == counts(baseline_status), "Restored demo counts differ from the baseline.")
        require(stable_board(client.request("/demo/api/board")) == stable_board(original_board), "Restored demo scores/ranks differ from the baseline.")
        report("demo_full_restore", passed=True, counts=counts(restored))

        client.demo_post("resume")
        require(not client.demo_status()["maintenance"], "Demo did not leave maintenance mode.")
        live_after = client.request("/api/status")
        require(counts(live_after) == counts(live_before)
                and live_after["state_id"] == live_before["state_id"]
                and live_after["maintenance"] == live_before["maintenance"]
                and live_after["controller"] == live_before["controller"],
                "Live event invariants changed during rehearsal; investigate without automatic repair.")
        report("complete", passed=True, live_unchanged=True, live=counts(live_after),
               demo=counts(restored), demo_resumed=True, synthetic=True)
    except Exception:
        if demo_started:
            try:
                client.demo_post("pause")
                report("failure_safety", demo_paused=True, live_repair_attempted=False)
            except Exception:
                report("failure_safety", demo_pause_confirmed=False, live_repair_attempted=False)
        raise
    finally:
        try:
            client.request("/api/logout", {})
        except Exception:
            report("logout", confirmed=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Loopback admin origin, such as http://127.0.0.1:8092")
    parser.add_argument("--password-file", type=Path, required=True, help="Private existing administrator password file")
    args = parser.parse_args()
    try:
        run(Client(args.url), private_password(args.password_file))
    except RehearsalError as exc:
        report("failed", passed=False, error=str(exc))
        raise SystemExit(1) from None
    except Exception as exc:
        # No traceback, arbitrary server content, cookie or credential is logged.
        report("failed", passed=False, error_type=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
