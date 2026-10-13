"""Root-only event enrollment and restricted SSH transport, not a public API.

Run with ``python -m ai4sci_judge.enrollment --config /private/config.json``.
The operator's org read key, SSH key and token master must never reach students
or judge workers. Student SSH keys must enforce the dedicated config receiver
and permit only the reverse loopback listener used below.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import random
import re
import select
import signal
import stat
import subprocess
import tempfile
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import base64

ACK = b"AI4SCI_ENROLLMENT_READY_V1\n"
INVENTORY_URL = "https://brevapi.us-west-2-prod.control-plane.brev.dev/api/organizations/{}/workspaces"
SSH_GATEWAY = "global.prd.ga.run.brev.nvidia.com"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
LOG = logging.getLogger("ai4sci.enrollment")
PROVISION_CODE = """import json,sys
from ai4sci_judge.store import Store
p=json.load(sys.stdin)
print(json.dumps(Store(p['state']).provision_workspace(p['key'],p['token'])))
"""


def private_read(path, owner_uid=0):
    """No symlinks, public files or another user's credential/config files."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Private paths must be absolute")
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != owner_uid or parent.st_mode & 0o077:
        raise ValueError("Private files need an owner-only parent directory")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o077 or info.st_size > 65536:
            raise ValueError("Private file must be owner-only, regular and bounded")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read(65537)
    finally:
        os.close(descriptor)


def load_config(path, owner_uid=0):
    config = json.loads(private_read(path, owner_uid))
    for key in ("org_id", "launchable_id", "audit_id", "judge_user", "ssh_user"):
        if not isinstance(config.get(key), str) or not IDENTIFIER.fullmatch(config[key]):
            raise ValueError("Invalid enrollment identifier")
    allowed = config.get("allowed_environment_ids", [])
    if not isinstance(allowed, list) or len(allowed) > 130 or any(not isinstance(v, str) or not IDENTIFIER.fullmatch(v) for v in allowed):
        raise ValueError("Optional workspace allowlist must contain at most 130 valid identifiers")
    config["allowed_environment_ids"] = allowed
    for key in ("state", "repo", "judge_python", "known_hosts_dir", "status_file"):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
            raise ValueError("Enrollment paths must be absolute")
    for key in ("known_hosts_dir", "status_file"):
        directory = Path(config[key]) if key == "known_hosts_dir" else Path(config[key]).parent
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o077:
            raise ValueError("Runtime metadata directories must be private")
    config["api_key"] = private_read(config["api_key_file"], owner_uid).decode("ascii").strip()
    config["token_master"] = private_read(config["token_master_file"], owner_uid)
    private_read(config["ssh_key_file"], owner_uid)
    if not config["api_key"] or any(c.isspace() for c in config["api_key"]) or len(config["token_master"]) < 32:
        raise ValueError("Invalid enrollment secrets")
    return config


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BrevInventory:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.workspaces = {}

    def request(self, url, payload=None):
        headers = {"Authorization": "Bearer " + self.config["api_key"]}
        if payload is not None:
            headers.update({"Content-Type": "application/json", "Connect-Protocol-Version": "1"})
        request = Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        with self.opener.open(request, timeout=25) as response:
            content = response.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024:
            raise ValueError("Inventory response exceeds limit")
        return json.loads(content)

    def inventory(self):
        payload = self.request(INVENTORY_URL.format(self.config["org_id"]))
        self.workspaces = {row["id"]: row for row in inventory_rows(payload) if isinstance(row.get("id"), str)}
        return payload

    def endpoint(self, identifier):
        # Org read keys cannot call Connect GetNetworkInfo. The org inventory
        # exposes the same gateway and public SSH port without broader scopes.
        return ssh_endpoint(self.workspaces[identifier], self.config)


def inventory_rows(payload):
    if isinstance(payload, dict):
        payload = payload.get("workspaces", payload.get("environments"))
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise ValueError("Unknown inventory schema")
    return payload


def inventory_candidates(payload, config):
    """Enroll only server-attested event Launchable instances with ready SSH.

    The optional operator allowlist may narrow this set, never bypass provenance.
    Names and client-written environment variables are not evidence of origin.
    """
    allowed, candidates = set(config.get("allowed_environment_ids", [])), {}
    for row in inventory_rows(payload):
        identifier = row.get("id")
        if (not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or
                identifier == config["audit_id"] or (allowed and identifier not in allowed)):
            continue
        environment = row.get("environment")
        labels = environment.get("labels") if isinstance(environment, dict) else None
        if (not isinstance(labels, dict) or labels.get("launchableId") != config["launchable_id"] or
                labels.get("launchableCreatedByOrgId") != config["org_id"] or
                labels.get("organizationId") != config["org_id"] or labels.get("environmentId") != identifier):
            continue
        if row.get("status") != "RUNNING" or row.get("verbBuildStatus") != "COMPLETED":
            continue
        try:
            ssh_endpoint(row, config)
        except ValueError:
            continue  # Fresh VMs briefly advertise a provider IP: wait for the gateway.
        candidates[identifier] = row
    if len(candidates) > 130:
        raise ValueError("Event workspace limit exceeded")
    return candidates


def ssh_endpoint(row, config):
    environment = row.get("environment") or {}
    labels = environment.get("labels") if isinstance(environment, dict) else None
    if (not isinstance(labels, dict) or row.get("organizationId") != config["org_id"] or
            labels.get("organizationId") != config["org_id"] or labels.get("environmentId") != row.get("id") or
            environment.get("environment_id") != row.get("id") or row.get("sshUser") != config["ssh_user"] or
            row.get("sshProxyHostname") not in (None, "")):
        raise ValueError("Unverified workspace SSH metadata")
    hostname, number = row.get("dns"), row.get("sshPort")
    if hostname != SSH_GATEWAY or type(number) is not int or not 1 <= number <= 65535:
        raise ValueError("Invalid trusted SSH endpoint")
    return hostname, number


def workspace_token(master, org_id, identifier):
    key = org_id + "/" + identifier
    digest = hmac.digest(master, b"ai4sci-workspace-v1\x00" + key.encode("ascii"), "sha256")
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def provision_account(config, identifier, token):
    payload = {"state": config["state"], "key": config["org_id"] + "/" + identifier, "token": token}
    result = subprocess.run(
        ["/usr/sbin/runuser", "-u", config["judge_user"], "--", config["judge_python"], "-c", PROVISION_CODE],
        cwd=config["repo"], input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1"}, timeout=45, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Account provisioning failed")
    person = json.loads(result.stdout)
    if not isinstance(person, dict) or set(person) != {"id", "nickname"}:
        raise ValueError("Unexpected account metadata")
    return person


def ssh_command(config, identifier, endpoint):
    alias = "ai4sci-" + hashlib.sha256((config["org_id"] + "/" + identifier).encode()).hexdigest()[:32]
    options = {
        "BatchMode": "yes", "IdentitiesOnly": "yes", "IdentityAgent": "none", "ForwardAgent": "no",
        "PasswordAuthentication": "no", "KbdInteractiveAuthentication": "no", "PreferredAuthentications": "publickey",
        "StrictHostKeyChecking": "accept-new", "HostKeyAlias": alias,
        "UserKnownHostsFile": str(Path(config["known_hosts_dir"]) / (alias + ".hosts")),
        "GlobalKnownHostsFile": "/dev/null", "ExitOnForwardFailure": "yes", "ServerAliveInterval": "15",
        "ServerAliveCountMax": "3", "ConnectTimeout": "20", "LogLevel": "ERROR",
    }
    args = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-i", config["ssh_key_file"]]
    for name, value in options.items():
        args += ["-o", name + "=" + value]
    # Request exec rather than a login shell so MOTD output cannot precede ACK.
    # authorized_keys overrides this fixed marker with the restricted receiver;
    # no command or arguments supplied by the inventory/student are executed.
    return args + ["-R", "127.0.0.1:8090:127.0.0.1:8090", "-p", str(endpoint[1]),
                   config["ssh_user"] + "@" + endpoint[0], "ai4sci-enrollment"]


def close_connection(process):
    if process.stdin:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    if process.stdout:
        process.stdout.close()


def open_connection(config, inventory, identifier):
    endpoint = inventory.endpoint(identifier)
    token = workspace_token(config["token_master"], config["org_id"], identifier)
    provision_account(config, identifier, token)
    process = subprocess.Popen(ssh_command(config, identifier, endpoint), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    try:
        process.stdin.write(json.dumps({"url": "http://127.0.0.1:8090", "token": token}).encode() + b"\n")
        process.stdin.flush()
        deadline, line = time.monotonic() + 45, b""
        while time.monotonic() < deadline and len(line) <= len(ACK):
            ready, _, _ = select.select([process.stdout], [], [], min(1, max(0, deadline - time.monotonic())))
            if ready:
                chunk = os.read(process.stdout.fileno(), 128)
                if not chunk:
                    break
                line += chunk
                if b"\n" in line:
                    if line == ACK:
                        return process  # Keep stdin open; EOF releases the forced receiver.
                    break
        raise RuntimeError("Restricted receiver did not acknowledge enrollment")
    except Exception:
        close_connection(process)
        raise


class EnrollmentDaemon:
    def __init__(self, config, inventory=None, connector=open_connection):
        self.config = config
        self.inventory = inventory or BrevInventory(config)
        self.connector = connector
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.connections, self.pending, self.retry, self.desired = {}, {}, {}, {}
        self.stopping = False

    def refresh_inventory(self):
        # Assign only after successful parsing: API failures must never evict sessions.
        self.desired = inventory_candidates(self.inventory.inventory(), self.config)

    def reconcile(self, now=None):
        now = time.monotonic() if now is None else now
        for identifier, process in list(self.connections.items()):
            if identifier not in self.desired or process.poll() is not None:
                close_connection(process)
                del self.connections[identifier]
                LOG.info("workspace=%s status=disconnected", identifier)
        for identifier, future in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[identifier]
            try:
                process = future.result()
                if identifier not in self.desired:
                    close_connection(process)
                    continue
                self.connections[identifier] = process
                self.retry.pop(identifier, None)
                LOG.info("workspace=%s status=connected", identifier)
            except Exception:
                attempts = min(self.retry.get(identifier, (0, 0))[0] + 1, 7)
                self.retry[identifier] = (attempts, now + min(120, 2 ** attempts) * random.uniform(0.8, 1.2))
                LOG.warning("workspace=%s status=retry_pending", identifier)
        for identifier in self.desired:
            if len(self.pending) >= 8 or len(self.connections) + len(self.pending) >= 130:
                break
            if identifier not in self.connections and identifier not in self.pending and now >= self.retry.get(identifier, (0, 0))[1]:
                self.pending[identifier] = self.pool.submit(self.connector, self.config, self.inventory, identifier)
        self.write_status()

    def write_status(self):
        target = Path(self.config["status_file"])
        status = {"updated": time.time(), "workspaces": {identifier: (
            "connected" if identifier in self.connections else "connecting" if identifier in self.pending else "retry_pending"
        ) for identifier in sorted(self.desired)}}
        with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, prefix=".enrollment-status-", delete=False) as output:
            temporary = Path(output.name)
            json.dump(status, output)
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def close(self):
        self.stopping = True
        self.pool.shutdown(wait=True, cancel_futures=True)
        for future in self.pending.values():
            if not future.cancelled() and future.exception() is None:
                close_connection(future.result())
        self.pending.clear()
        for process in self.connections.values():
            close_connection(process)
        self.connections.clear()
        self.desired.clear()
        self.write_status()

    def run(self):
        next_inventory = 0
        try:
            while not self.stopping:
                now = time.monotonic()
                if now >= next_inventory:
                    try:
                        self.refresh_inventory()
                    except Exception:
                        LOG.warning("status=inventory_retry_pending")
                    next_inventory = now + 20
                self.reconcile(now)
                time.sleep(1)
        finally:
            self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Enrollment must run as root with private operator credentials")
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    daemon = EnrollmentDaemon(load_config(args.config))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: setattr(daemon, "stopping", True))
    daemon.run()


if __name__ == "__main__":
    main()
