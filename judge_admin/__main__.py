"""Operator-only service and backup CLI; never initialize production implicitly."""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import threading


def main():
    parser = argparse.ArgumentParser(description="Private judge administration")
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("backup")
    password = commands.add_parser("init-password")
    password.add_argument("--file", type=Path, required=True)
    password_store = commands.add_parser("init-password-store")
    password_store.add_argument("--file", type=Path, required=True)
    password_store.add_argument("--stdin", action="store_true", required=True,
                                help="Read one password from private standard input; never pass it as an argument.")
    demo = commands.add_parser("init-demo")
    demo.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "init-password-store":
        from .auth import PasswordStore, _private_path
        if sys.stdin.isatty():
            parser.error("Use private redirected standard input, not an echoing terminal.")
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096:
            parser.error("Password input is too long.")
        try:
            value = raw.decode("utf-8")
        except UnicodeError:
            parser.error("Password input must be UTF-8.")
        if value.endswith("\n"):
            value = value[:-1]
            if value.endswith("\r"):
                value = value[:-1]
        target = _private_path(args.file)
        _private_path(target.parent, directory=True)
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        PasswordStore.initialize(target, value)
        print("Administrator password verifier initialized. No plaintext password is stored.")
        return
    if args.command == "init-password":
        from .storage import _safe_path
        _safe_path(args.file, private=False)
        args.file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(args.file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(secrets.token_urlsafe(32) + "\n")
        print("Administrator password created in the private file. Never publish it.")
        return
    if args.command == "init-demo":
        from .demo import initialize_demo
        initialize_demo(args.state)
        print("Isolated demo initialized. All scores are synthetic display fixtures, not GPU grading results.")
        return
    if not args.config:
        parser.error("--config is required for serve and backup")
    from .storage import AdminStorage, _safe_path
    from .control import SystemdController, MemoryController
    config_path = _safe_path(args.config, private=False)
    if config_path.stat().st_mode & 0o022:
        raise ValueError("Administrator configuration must not be group/world writable.")
    config = json.loads(config_path.read_text())
    if config.get("secure_link") is not None and config_path.stat().st_uid != 0:
        raise ValueError("Only a root-owned administrator configuration may enable a Secure Link.")
    directories = [_safe_path(config[key], directory=True) for key in
                   ("live_state", "live_backups", "demo_state", "demo_backups")]
    if any(first.is_relative_to(second) or second.is_relative_to(first)
           for index, first in enumerate(directories) for second in directories[index + 1:]):
        raise ValueError("Live/demo state and backup directories must be separate and non-nested.")
    live = AdminStorage(config["live_state"], config["live_backups"], SystemdController())
    if args.command == "backup":
        result = live.create_backup(reason="scheduled")
        print(json.dumps({"backup": result["id"], "status": "created"}))
        return
    demo_path = Path(config["demo_state"])
    if not _safe_path(demo_path / "DEMO_ONLY").is_file():
        raise ValueError("A separate explicitly initialized DEMO_ONLY state is required.")
    demo = AdminStorage(demo_path, config["demo_backups"], MemoryController())
    credential_store = None
    password_value = None
    if "password_store" in config:
        from .auth import PasswordStore
        # Explicit store configuration is authoritative. A corrupt or missing
        # store must never resurrect the old systemd bootstrap credential.
        credential_store = PasswordStore(config["password_store"])
    else:
        secret_path = _safe_path(config["password_file"])
        if secret_path.stat().st_mode & 0o077:
            raise ValueError("Administrator password file must be owner-readable only.")
        password_value = secret_path.read_text().strip()
    from .web import create_server
    server = create_server(live, demo, password_value, config.get("port", 8092),
                           config.get("projector_url", "http://localhost:8091/display"),
                           secure_link=config.get("secure_link"), credential_store=credential_store)
    print(f"Private administrator: http://127.0.0.1:{server.server_port}/", flush=True)
    secure_server = server.secure_server
    secure_thread = None
    if secure_server is not None:
        secure_thread = threading.Thread(target=secure_server.serve_forever, daemon=True)
        secure_thread.start()
        print(f"Administrator Secure Link: https://{secure_server.secure_link_hostname}/", flush=True)
    # shutdown() must run outside the thread executing serve_forever().
    previous_term = signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        if secure_server is not None:
            secure_server.shutdown()
            secure_server.server_close()
            secure_thread.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
