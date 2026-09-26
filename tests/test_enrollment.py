"""Enrollment trusts operator inventory, not student assertions or public names."""
from concurrent.futures import Future
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ai4sci_judge import enrollment as module


@pytest.fixture
def config(tmp_path):
    private = tmp_path / "enrollment"
    private.mkdir(mode=0o700)
    hosts = private / "known-hosts"
    hosts.mkdir(mode=0o700)
    return {"org_id": "org-event", "launchable_id": "env-template", "audit_id": "audit-vm",
            "state": str(tmp_path / "state"), "judge_user": "ai4sci-judge", "ssh_user": "ubuntu",
            "judge_python": "/srv/judge/venv/bin/python", "repo": "/srv/judge/current",
            "api_key": "PRIVATE_API_KEY", "token_master": b"a" * 32,
            "api_key_file": str(private / "api-key"), "token_master_file": str(private / "token-master"),
            "ssh_key_file": str(private / "id_ed25519"), "known_hosts_dir": str(hosts),
            "status_file": str(private / "status.json"), "allowed_environment_ids": ["vm-a", "vm-b"]}


def saved_config(config):
    for key, data in (("api_key_file", b"PRIVATE_API_KEY"), ("token_master_file", b"a" * 32), ("ssh_key_file", b"PRIVATE_SSH_KEY")):
        path = Path(config[key])
        path.write_bytes(data)
        path.chmod(0o600)
    path = Path(config["status_file"]).parent / "config.json"
    path.write_text(json.dumps({k: v for k, v in config.items() if k not in {"api_key", "token_master"}}))
    path.chmod(0o600)
    return path


def rows(*identifiers):
    result = []
    for identifier in identifiers:
        row = network(id=identifier, status="RUNNING", verbBuildStatus="COMPLETED")
        row["environment"]["environment_id"] = identifier
        row["environment"]["labels"].update({"environmentId": identifier, "launchableId": "env-template",
                                             "launchableCreatedByOrgId": "org-event"})
        result.append(row)
    return result


def network(**overrides):
    return {"dns": "global.prd.ga.run.brev.nvidia.com", "sshPort": 42467, "sshUser": "ubuntu",
            "id": "vm-a", "organizationId": "org-event", "sshProxyHostname": "",
            "environment": {"environment_id": "vm-a", "labels": {"organizationId": "org-event", "environmentId": "vm-a"}}, **overrides}


def test_private_config_loads_only_owner_private_files(config):
    path = saved_config(config)
    loaded = module.load_config(path, owner_uid=os.geteuid())
    assert loaded["api_key"] == "PRIVATE_API_KEY"
    assert loaded["token_master"] == b"a" * 32
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        module.load_config(path, owner_uid=os.geteuid())


def test_symlink_and_public_parent_credentials_are_rejected(config):
    path = saved_config(config)
    link = path.parent / "linked.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        module.load_config(link, owner_uid=os.geteuid())
    path.parent.chmod(0o755)
    with pytest.raises(ValueError, match="parent directory"):
        module.load_config(path, owner_uid=os.geteuid())


@pytest.mark.parametrize("change", [{"allowed_environment_ids": None}, {"allowed_environment_ids": ["x"] * 131},
                                    {"allowed_environment_ids": ["../x"]}, {"org_id": "x/y"},
                                    {"repo": "relative"}, {"judge_user": "-u"}])
def test_config_validation_is_closed_and_bounded(config, change):
    config.update(change)
    with pytest.raises(ValueError):
        module.load_config(saved_config(config), owner_uid=os.geteuid())


def test_inventory_requires_allowlisted_vm_in_exact_org_and_running_state(config):
    payload = rows("vm-a", "vm-b", "audit-vm", "unapproved")
    payload[1]["organizationId"] = "other-org"
    config["allowed_environment_ids"].append("audit-vm")
    assert set(module.inventory_candidates(payload, config)) == {"vm-a"}
    payload[0]["status"] = "STOPPED"
    assert module.inventory_candidates({"workspaces": payload}, config) == {}
    # Being named after the Launchable does not establish trusted provenance.
    assert module.inventory_candidates([{**rows("unapproved")[0], "name": "env-template"}], config) == {}


def test_verified_launchable_automatically_enrolls_future_instances_without_allowlist(config):
    config.pop("allowed_environment_ids")
    assert set(module.inventory_candidates(rows("new-vm", "future-vm"), config)) == {"new-vm", "future-vm"}
    config["allowed_environment_ids"] = []
    assert set(module.inventory_candidates(rows("new-vm"), config)) == {"new-vm"}


@pytest.mark.parametrize("field,value", [("launchableId", "other-template"), ("launchableId", None),
    ("launchableCreatedByOrgId", "other-org"), ("launchableCreatedByOrgId", None),
    ("organizationId", "other-org"), ("environmentId", "other-vm")])
def test_explicit_allowlist_cannot_bypass_missing_or_wrong_launchable_provenance(config, field, value):
    payload = rows("vm-a")
    payload[0]["environment"]["labels"][field] = value
    assert module.inventory_candidates(payload, config) == {}


@pytest.mark.parametrize("change", [{"environment": {}}, {"environment": None}, {"status": "STOPPED"},
    {"verbBuildStatus": "BUILDING"}, {"verbBuildStatus": None}, {"dns": "136.70.132.5", "sshPort": 22},
    {"sshPort": 0}, {"organizationId": "other-org"}, {"sshProxyHostname": "other"}])
def test_automatic_enrollment_waits_for_valid_completed_gateway_metadata(config, change):
    config["allowed_environment_ids"] = []
    payload = rows("vm-a")
    payload[0].update(change)
    assert module.inventory_candidates(payload, config) == {}


@pytest.mark.parametrize("payload", [None, {}, {"error": "unauthorized"}, {"workspaces": {}}, ["vm-a"]])
def test_unknown_inventory_schema_is_not_interpreted_as_empty(payload, config):
    with pytest.raises(ValueError, match="schema"):
        module.inventory_candidates(payload, config)


def test_endpoint_uses_exact_ssh_destination_and_valid_gateway(config):
    assert module.ssh_endpoint(network(), config) == ("global.prd.ga.run.brev.nvidia.com", 42467)
    payload = network()
    payload["environment"]["instance"] = {"ssh_port": 22, "public_ip": "ignored"}
    assert module.ssh_endpoint(payload, config)[1] == 42467


@pytest.mark.parametrize("payload", [{}, network(environment={}), network(organizationId="other-org"),
    network(environment={"environment_id": "vm-b", "labels": {"organizationId": "org-event", "environmentId": "vm-a"}}),
    network(environment={"environment_id": "vm-a", "labels": {"organizationId": "other-org", "environmentId": "vm-a"}}),
    network(dns="evil.example"), network(dns="global.brev.nvidia.com.evil.example"),
    network(dns="-ProxyCommand=bad"), network(sshPort="42467"), network(sshPort=True),
    network(sshPort=0), network(sshPort=65536), network(sshUser="root"), network(sshProxyHostname="other")])
def test_untrusted_or_ambiguous_endpoints_rejected(payload, config):
    with pytest.raises(ValueError):
        module.ssh_endpoint(payload, config)


def test_token_is_stable_domain_separated_and_private():
    token = module.workspace_token(b"a" * 32, "org-a", "vm-a")
    assert len(token) == 43 and token == module.workspace_token(b"a" * 32, "org-a", "vm-a")
    assert len({token, module.workspace_token(b"a" * 32, "org-b", "vm-a"),
                module.workspace_token(b"a" * 32, "org-a", "vm-b"),
                module.workspace_token(b"b" * 32, "org-a", "vm-a")}) == 4


def test_provisioning_runs_as_judge_and_sends_secrets_only_over_stdin(config, monkeypatch):
    captured = {}

    def run(args, **kwargs):
        captured.update({"args": args, **kwargs})
        return SimpleNamespace(returncode=0, stdout=b'{"id":"person","nickname":null}')

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.provision_account(config, "vm-a", "PRIVATE_TOKEN") == {"id": "person", "nickname": None}
    assert captured["args"][:5] == ["/usr/sbin/runuser", "-u", "ai4sci-judge", "--", config["judge_python"]]
    assert "PRIVATE_TOKEN" not in repr(captured["args"])
    assert json.loads(captured["input"]) == {"state": config["state"], "key": "org-event/vm-a", "token": "PRIVATE_TOKEN"}
    assert captured["stderr"] == subprocess.DEVNULL
    assert "PRIVATE_API_KEY" not in repr(captured)


def test_ssh_uses_only_dedicated_identity_no_shell_and_per_vm_host_pins(config):
    command = module.ssh_command(config, "vm-a", module.ssh_endpoint(network(), config))
    text = " ".join(command)
    assert command[-2:] == ["ubuntu@global.prd.ga.run.brev.nvidia.com", "ai4sci-enrollment"]
    assert "-F /dev/null" in text and "-T" in command
    assert "IdentityAgent=none" in text and "IdentitiesOnly=yes" in text
    assert "StrictHostKeyChecking=accept-new" in text
    assert "ExitOnForwardFailure=yes" in text
    assert "127.0.0.1:8090:127.0.0.1:8090" in command
    assert "ServerAliveInterval=15" in text and "ServerAliveCountMax=3" in text
    assert config["ssh_key_file"] in command and "PRIVATE_API_KEY" not in text
    assert command != module.ssh_command(config, "vm-b", module.ssh_endpoint(network(), config))


def test_read_only_inventory_supplies_endpoint_without_connect_api(config, monkeypatch):
    inventory = module.BrevInventory(config)
    requests = []

    def request(url, payload=None):
        requests.append((url, payload))
        return [network()]

    monkeypatch.setattr(inventory, "request", request)
    inventory.inventory()
    assert inventory.endpoint("vm-a") == (module.SSH_GATEWAY, 42467)
    assert requests == [(module.INVENTORY_URL.format("org-event"), None)]


@pytest.mark.parametrize("valid_ack", [True, False])
def test_receiver_acknowledgement_keeps_stdin_open_only_after_exact_protocol(config, monkeypatch, valid_ack):
    original_popen = subprocess.Popen
    calls, processes = [], []

    def popen(command, **kwargs):
        calls.append(command)
        response = module.ACK.decode() if valid_ack else "WRONG_ACK\n"
        code = ("import json,sys\n"
                "p=json.loads(sys.stdin.readline())\n"
                "assert set(p)=={'url','token'} and p['url']=='http://127.0.0.1:8090' and len(p['token'])==43\n"
                f"sys.stdout.write({response!r});sys.stdout.flush()\n"
                "sys.stdin.read()\n")
        child = original_popen([sys.executable, "-c", code], **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module, "provision_account", lambda *args: {"id": "p", "nickname": None})
    inventory = SimpleNamespace(endpoint=lambda identifier: (module.SSH_GATEWAY, 42467))
    if valid_ack:
        child = module.open_connection(config, inventory, "vm-a")
        assert child.poll() is None and not child.stdin.closed
        module.close_connection(child)
    else:
        with pytest.raises(RuntimeError, match="acknowledge"):
            module.open_connection(config, inventory, "vm-a")
        assert processes[0].poll() is not None and processes[0].stdin.closed
    assert calls[0][0] == "/usr/bin/ssh"
    assert "PRIVATE_API_KEY" not in repr(calls)


class Process:
    def __init__(self):
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
        self.code, self.terminated = None, False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, 0

    def wait(self, timeout):
        return self.code


class Pool:
    def __init__(self):
        self.calls, self.futures = [], []

    def submit(self, fn, *args):
        future = Future()
        self.calls.append((fn, args))
        self.futures.append(future)
        return future

    def shutdown(self, wait, cancel_futures):
        for future in self.futures:
            future.cancel()


def daemon(config, monkeypatch, payload):
    pool = Pool()
    monkeypatch.setattr(module, "ThreadPoolExecutor", lambda max_workers: pool)
    inventory = SimpleNamespace(inventory=lambda: payload)
    return module.EnrollmentDaemon(config, inventory=inventory), pool


def test_inventory_failure_keeps_connected_vms_but_successful_removal_closes(config, monkeypatch):
    instance, _ = daemon(config, monkeypatch, rows("vm-a"))
    instance.refresh_inventory()
    connection = Process()
    instance.connections["vm-a"] = connection
    instance.inventory.inventory = lambda: {"error": "PRIVATE_API_KEY"}
    with pytest.raises(ValueError):
        instance.refresh_inventory()
    instance.reconcile(now=100)
    assert not connection.terminated and "vm-a" in instance.connections
    instance.inventory.inventory = lambda: []
    instance.refresh_inventory()
    instance.reconcile(now=101)
    assert connection.terminated and not instance.connections
    instance.close()


def test_connection_retries_are_bounded_and_success_preserves_socket(config, monkeypatch, caplog):
    instance, pool = daemon(config, monkeypatch, rows("vm-a"))
    instance.refresh_inventory()
    instance.reconcile(now=100)
    assert len(pool.calls) == 1
    pool.futures[0].set_exception(RuntimeError("PRIVATE_TOKEN"))
    instance.reconcile(now=101)
    assert len(pool.calls) == 1 and 101 < instance.retry["vm-a"][1] < 105
    instance.reconcile(now=1000)
    assert len(pool.calls) == 2
    connection = Process()
    pool.futures[1].set_result(connection)
    instance.reconcile(now=1001)
    assert instance.connections["vm-a"] is connection and not connection.stdin.closed
    assert "vm-a" not in instance.retry and "PRIVATE_TOKEN" not in caplog.text
    status = Path(config["status_file"]).read_text()
    assert json.loads(status)["workspaces"] == {"vm-a": "connected"}
    assert "PRIVATE_TOKEN" not in status and "PRIVATE_API_KEY" not in status
    assert Path(config["status_file"]).stat().st_mode & 0o077 == 0
    instance.close()
    assert connection.terminated


def test_deleted_workspace_cannot_gain_late_successful_connection(config, monkeypatch):
    instance, pool = daemon(config, monkeypatch, rows("vm-a"))
    instance.refresh_inventory()
    instance.reconcile(now=100)
    connection = Process()
    pool.futures[0].set_result(connection)
    instance.inventory.inventory = lambda: []
    instance.refresh_inventory()
    instance.reconcile(now=101)
    assert connection.terminated and not instance.connections
    instance.close()


def test_only_eight_provisioning_attempts_run_at_once(config, monkeypatch):
    config["allowed_environment_ids"] = [f"vm-{i}" for i in range(130)]
    instance, pool = daemon(config, monkeypatch, rows(*config["allowed_environment_ids"]))
    instance.refresh_inventory()
    instance.reconcile(now=100)
    assert len(instance.pending) == len(pool.calls) == 8
    instance.reconcile(now=101)
    assert len(pool.calls) == 8
    instance.close()
