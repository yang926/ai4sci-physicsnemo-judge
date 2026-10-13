"""Privileged gate tests never run root, systemd, or access live state."""
import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "ops" / "ai4sci-judge-control"
LOADER = importlib.machinery.SourceFileLoader("admin_control", str(PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
control = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(control)


def states(intake=False, workers=False):
    def value(unit):
        stopped = intake if unit in control.INTAKE else workers
        return {"active_state": "inactive" if stopped else "active",
                "sub_state": "dead" if stopped else "running"}
    return {"intake_paused": intake, "workers_stopped": workers,
            "services": {unit: value(unit) for unit in control.UNITS}}


def test_status_only_reads_ten_fixed_units(monkeypatch):
    def invoke(args, timeout):
        assert args == ["show", "--property=Id,LoadState,ActiveState,SubState", *control.UNITS]
        assert timeout == 15
        return "\n\n".join("Id=%s\nLoadState=loaded\nActiveState=inactive\nSubState=dead" % unit
                             for unit in control.UNITS) + "\n"
    monkeypatch.setattr(control, "invoke", invoke)
    assert control.status() == states(True, True)


@pytest.mark.parametrize("output", ["", "Id=unknown.service\nLoadState=loaded",
    "Id=ai4sci-judge-api.service\nLoadState=not-found\nActiveState=inactive\nSubState=dead"])
def test_status_fails_closed_for_missing_units(monkeypatch, output):
    monkeypatch.setattr(control, "invoke", lambda *a, **kw: output)
    with pytest.raises(control.ControlError):
        control.status()


@pytest.mark.parametrize("args", [[], ["status", "--help"], ["stop"], ["status;id"],
                                  ["pause", "other.service"], ["/bin/sh"]])
def test_argument_allowlist_precedes_root_or_service_access(monkeypatch, args, capsys):
    monkeypatch.setattr(control, "operate", lambda verb: pytest.fail("must not invoke"))
    assert control.main(args) == 1
    assert "exactly one verb" in capsys.readouterr().err


def test_non_root_call_is_rejected(monkeypatch, capsys):
    monkeypatch.setattr(control.os, "geteuid", lambda: 12345)
    assert control.main(["status"]) == 1
    assert "requires the fixed sudo rule" in capsys.readouterr().err


def test_pause_stops_only_intake_and_checks_result(monkeypatch):
    commands = []
    monkeypatch.setattr(control, "invoke", lambda args: commands.append(args))
    monkeypatch.setattr(control, "status", lambda: states(True, False))
    assert control.operate("pause")["intake_paused"]
    assert commands == [["stop", *control.INTAKE]]


def test_quiesce_refuses_active_intake(monkeypatch):
    monkeypatch.setattr(control, "status", lambda: states(False, False))
    monkeypatch.setattr(control, "invoke", lambda args: pytest.fail("must not stop workers"))
    with pytest.raises(control.ControlError, match="pause intake"):
        control.operate("quiesce")


def test_quiesce_stops_only_workers_after_intake_pause(monkeypatch):
    commands, results = [], iter([states(True, False), states(True, True)])
    monkeypatch.setattr(control, "status", lambda: next(results))
    monkeypatch.setattr(control, "invoke", lambda args: commands.append(args))
    assert control.operate("quiesce")["workers_stopped"]
    assert commands == [["stop", *control.WORKERS]]


def test_resume_starts_all_fixed_units_but_never_projector(monkeypatch):
    commands = []
    monkeypatch.setattr(control, "invoke", lambda args: commands.append(args))
    monkeypatch.setattr(control, "status", lambda: states(False, False))
    control.operate("resume")
    assert commands == [["start", *control.WORKERS, *control.INTAKE]]
    assert not any("display" in unit for unit in commands[0])


@pytest.mark.parametrize("verb,result", [("pause", states()),
                                        ("quiesce", states(True, False)),
                                        ("resume", states(True, True))])
def test_failed_postconditions_never_report_success(monkeypatch, verb, result):
    monkeypatch.setattr(control, "status", lambda: result)
    monkeypatch.setattr(control, "invoke", lambda args: None)
    with pytest.raises(control.ControlError):
        control.operate(verb)


def test_systemctl_is_absolute_no_shell_and_sanitized(monkeypatch):
    def run(args, **kwargs):
        assert args == ["/usr/bin/systemctl", "--no-ask-password", "--no-pager", "stop", *control.INTAKE]
        assert "shell" not in kwargs and kwargs["cwd"] == "/"
        assert kwargs["env"] == control.ENV and kwargs["timeout"] == 120
        assert kwargs["stdin"] == control.subprocess.DEVNULL
        return SimpleNamespace(returncode=0, stdout=b"")
    monkeypatch.setattr(control.subprocess, "run", run)
    assert control.invoke(["stop", *control.INTAKE]) == ""


@pytest.mark.parametrize("failure", [SimpleNamespace(returncode=1, stdout=b"private error"),
                                     SimpleNamespace(returncode=0, stdout=b"x" * 16385)])
def test_failure_and_oversized_output_are_not_forwarded(monkeypatch, failure):
    monkeypatch.setattr(control.subprocess, "run", lambda *a, **kw: failure)
    with pytest.raises(control.ControlError, match="service command failed"):
        control.invoke(["stop", *control.INTAKE])


def test_sudoers_only_allows_four_exact_commands():
    lines = (PATH.parent / "ai4sci-judge-admin.sudoers").read_text().splitlines()
    rules = [line for line in lines if line and not line.startswith("#")]
    assert len(rules) == 1 and rules[0].startswith("ai4sci-judge ALL=(root) NOPASSWD: ")
    allowed = rules[0].split("NOPASSWD: ", 1)[1].split(", ")
    assert set(allowed) == {"/usr/local/sbin/ai4sci-judge-control " + verb for verb in control.VERBS}


def test_admin_unit_does_not_disable_its_fixed_sudo_capability():
    text = (PATH.parent / "ai4sci-judge-admin.service").read_text()
    directives = dict(line.split("=", 1) for line in text.splitlines()
                      if "=" in line and not line.startswith("#"))
    # These seccomp/sandbox directives implicitly enable NoNewPrivileges in
    # deployed systemd249 even if `systemctl show` reports the default false.
    forbidden = {"NoNewPrivileges", "RestrictSUIDSGID", "RestrictAddressFamilies",
                 "ProtectKernelTunables", "ProtectKernelModules", "PrivateDevices",
                 "SystemCallFilter", "SystemCallArchitectures", "RestrictNamespaces",
                 "RestrictRealtime", "LockPersonality", "MemoryDenyWriteExecute"}
    assert not (forbidden & directives.keys())
    assert directives["LoadCredential"] == "admin-password:/etc/ai4sci-judge-admin/admin-password"
    assert directives["ProtectSystem"] == "strict"
