"""Implementation scoring is complete before (and survives) optional feedback."""
import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai4sci_judge.catalog import CHALLENGES, rules
from ai4sci_judge import evaluate as evaluator
from ai4sci_judge import result_checkpoint as checkpoints
from ai4sci_judge import worker
from ai4sci_judge.store import Store
from tests.test_judge import answer
from tests.test_judge_operators import operator_answer


def sources(challenge):
    return {name: operator_answer(index) if challenge == "4" else answer(challenge, name)
            for index, name in enumerate(CHALLENGES[challenge]["files"], 1)}


def payload(challenge="1"):
    return {"challenge": challenge, "sources": sources(challenge), "settings": rules(2),
            "run_nonce": "a" * 64}


@pytest.mark.parametrize("challenge", CHALLENGES)
def test_preflight_uses_same_checks_for_all_eleven_levels_without_training(challenge, monkeypatch):
    def forbid(*args, **kwargs):
        pytest.fail("Preflight must not train a model")
    monkeypatch.setattr(evaluator, "run_pinn", forbid)
    monkeypatch.setattr(evaluator, "run_operator", forbid)
    request = payload(challenge)
    result = evaluator.preflight(challenge, request["sources"], request["settings"])
    assert result["score"] == 100
    assert result["implementation_assessment_complete"] is True
    assert result["feedback_status"] == "not_requested"
    for level in result["levels"].values():
        assert level["status"] == "implementation_checked"
        assert level["feedback_status"] == "not_requested"
        assert all(level["components"].values())
        assert level["evaluation_errors"] == {}
        assert "feedback_seconds" not in level
    assert "elapsed_seconds" not in result
    checkpoints.validate_result(result, request)


def test_omitted_incorrect_and_unfinished_levels_have_distinct_feedback():
    request = payload()
    del request["sources"]["wave_l3.py"]
    request["sources"]["wave_l1.py"] = request["sources"]["wave_l1.py"].replace(
        'return {"wave": u.diff(t, 2) - c**2 * (u.diff(x, 2) + u.diff(y, 2))}',
        'return {"wave": 0}')
    # Replace the function body independently of source formatting.
    original = request["sources"]["wave_l1.py"]
    setup = original[original.index("def student_conditions"):]
    request["sources"]["wave_l1.py"] = 'def student_equations(x,y,t,u,c=1.0):\n    return {"wave": 0}\n' + setup
    request["sources"]["wave_l2.py"] = 'def student_equations(x,y,t,u,c=1.0):\n    raise NotImplementedError\n'
    result = evaluator.preflight("1", request["sources"], request["settings"])
    wrong, unfinished, omitted = (result["levels"][f"wave_l{i}.py"] for i in (1, 2, 3))
    assert wrong["status"] == "incorrect_implementation"
    assert wrong["components"]["wave"] is False and wrong["messages"]["wave"]
    assert 0 < wrong["score"] < 100
    assert unfinished["status"] == "invalid" and unfinished["score"] == 0
    assert omitted["status"] == "not_submitted" and omitted["score"] == 0
    checkpoints.validate_result(result, request)


def test_training_failure_keeps_every_assessed_level_and_does_not_claim_feedback_pass(tmp_path, monkeypatch):
    snapshots, calls = [], []
    ticks = iter((10., 12., 20., 23., 30., 34.))
    monkeypatch.setattr(evaluator, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    def fail(prepared, challenge, filename, settings, directory, device):
        # Even the last Level was checked and saved before the first train call.
        assert set(snapshots[0]["levels"]) == set(CHALLENGES["1"]["files"])
        assert all(level["implementation_points"] == 100 for level in snapshots[0]["levels"].values())
        calls.append(filename)
        raise RuntimeError("private diagnostic /sensitive/path")
    monkeypatch.setattr(evaluator, "run_pinn", fail)
    request = payload()
    result = evaluator.evaluate("1", request["sources"], request["settings"], tmp_path, "cpu",
                                lambda result: snapshots.append(copy.deepcopy(result)))
    assert len(calls) == 3 and result["score"] == 100
    assert result["feedback_status"] == "failed"
    assert all(level["feedback_status"] == "failed" for level in result["levels"].values())
    assert all(level["status"] == "implementation_checked" for level in result["levels"].values())
    assert [level["feedback_seconds"] for level in result["levels"].values()] == [2., 3., 4.]
    assert "/sensitive/path" not in json.dumps(result)
    checkpoints.validate_result(result, request)


def test_success_feedback_duration_is_measured_not_taken_from_student_metrics(tmp_path, monkeypatch):
    request = payload()
    request["sources"] = {"wave_l1.py": request["sources"]["wave_l1.py"]}
    ticks = iter((10., 12.3456789))
    monkeypatch.setattr(evaluator, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    metrics = {"pde_rmse": 0., "initial_displacement_rmse": 0., "initial_velocity_rmse": 0.,
               "boundary_rmse": 0., "reference_over_time": {"relative_l2": 0.}, "training_seconds": 999999.}
    monkeypatch.setattr(evaluator, "run_pinn", lambda *args: metrics)
    result = evaluator.evaluate("1", request["sources"], request["settings"], tmp_path, "cpu")
    assert result["levels"]["wave_l1.py"]["feedback_seconds"] == 2.345679
    assert "feedback_seconds" not in result["levels"]["wave_l2.py"]
    checkpoints.validate_result(result, request)
    for bad in (True, -1, float("nan"), float("inf"), None, "2"):
        modified = copy.deepcopy(result)
        modified["levels"]["wave_l1.py"]["feedback_seconds"] = bad
        with pytest.raises(ValueError, match="duration"):
            checkpoints.validate_result(modified, request)
        modified = copy.deepcopy(result)
        modified["elapsed_seconds"] = bad
        with pytest.raises(ValueError, match="elapsed time"):
            checkpoints.validate_result(modified, request)


def test_checkpoint_rejects_wrong_attempt_tampering_missing_levels_and_symlinks(tmp_path):
    request = payload()
    result = evaluator.preflight("1", request["sources"], request["settings"])
    path = tmp_path / "checkpoint.json"
    checkpoints.write_checkpoint(path, request, result)
    other = {**request, "run_nonce": "b" * 64}
    with pytest.raises(ValueError, match="different attempt"):
        checkpoints.recover_checkpoint(path, other, "failed")
    for change in (lambda r: r.update(score=99),
                   lambda r: r["levels"].pop("wave_l3.py"),
                   lambda r: r["levels"]["wave_l1.py"].update(quality_points=1),
                   lambda r: r["levels"]["wave_l1.py"]["components"].update(wave=1)):
        invalid = copy.deepcopy(result)
        change(invalid)
        checkpoints.write_checkpoint(path, request, invalid)
        with pytest.raises(ValueError):
            checkpoints.recover_checkpoint(path, request, "failed")
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        checkpoints.recover_checkpoint(link, request, "failed")


def test_checkpoint_is_atomic_if_replace_fails(tmp_path, monkeypatch):
    request = payload()
    result = evaluator.preflight("1", request["sources"], request["settings"])
    path = tmp_path / "checkpoint.json"
    checkpoints.write_checkpoint(path, request, result)
    before = path.read_bytes()
    monkeypatch.setattr(checkpoints.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk error")))
    with pytest.raises(OSError):
        checkpoints.write_checkpoint(path, request, {**result, "score": 0})
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_preflight_cli_outputs_only_assessment_without_runs_directory(tmp_path):
    request = payload("4")
    input_path, output = tmp_path / "request.json", tmp_path / "result.json"
    input_path.write_text(json.dumps(request))
    process = subprocess.run([sys.executable, "-m", "ai4sci_judge.evaluate", "--preflight-only",
                              "--input", str(input_path), "--output", str(output)],
                             capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stderr
    result = checkpoints.validate_result(json.loads(output.read_text()), request)
    assert result["score"] == 100 and result["feedback_status"] == "not_requested"
    assert set(tmp_path.iterdir()) == {input_path, output}


def test_recovered_partial_attempt_never_replaces_a_higher_previous_best(tmp_path):
    store = Store.initialize(tmp_path / "judge", steps=2)
    participant = store.authenticate(store.add_participant("Previous best"))["id"]
    complete = payload()
    store.submit(participant, "1", complete["sources"])
    first = store.claim()
    score = evaluator.preflight("1", complete["sources"], complete["settings"])
    store.finish(first, result=score)
    partial = {**complete, "sources": {"wave_l1.py": complete["sources"]["wave_l1.py"]}, "run_nonce": "b" * 64}
    store.submit(participant, "1", partial["sources"], cooldown=0)
    second = store.claim()
    score = evaluator.preflight("1", partial["sources"], partial["settings"])
    score["feedback_status"] = "pending"
    score["levels"]["wave_l1.py"]["feedback_status"] = "pending"
    path = tmp_path / "partial-checkpoint.json"
    checkpoints.write_checkpoint(path, partial, score)
    recovered = checkpoints.recover_checkpoint(path, partial, "time_limit")
    store.finish(second, result=recovered)
    assert recovered["score"] == 33.33
    assert store.board()["participants"][0]["scores"]["1"] == 100
    assert len(store.history(participant)) == 2


@pytest.mark.parametrize("failure,checkpoint", [("crash", True), ("timeout", True),
                                               ("crash", False), ("timeout", False)])
def test_worker_recovers_only_completed_bound_implementation_assessment(tmp_path, monkeypatch, failure, checkpoint):
    store = Store.initialize(tmp_path / "judge", steps=2)
    participant = store.authenticate(store.add_participant("Resilience test"))["id"]
    store.submit(participant, "1", sources("1"))
    killed = []
    class Process:
        pid = 123456789
        returncode = 1
        def __init__(self, command, **kwargs):
            request = json.loads(Path(command[command.index("--input") + 1]).read_text())
            if checkpoint:
                result = evaluator.preflight("1", request["sources"], request["settings"])
                result["feedback_status"] = "running"
                for level in result["levels"].values():
                    level["feedback_status"] = "pending"
                checks_path = Path(command[command.index("--checkpoint") + 1])
                checkpoints.write_checkpoint(checks_path, request, result)
        def wait(self, timeout=None):
            if timeout is not None and failure == "timeout":
                raise subprocess.TimeoutExpired("trusted evaluator", timeout)
            return self.returncode
        def poll(self):
            return self.returncode
    monkeypatch.setattr(worker.subprocess, "Popen", Process)
    monkeypatch.setattr(worker.os, "killpg", lambda pid, sig: killed.append(pid))
    assert worker.work_once(store)
    row = store.history(participant)[0]
    if checkpoint:
        assert row["status"] == "completed" and row["score"] == 100
        expected = "time_limit" if failure == "timeout" else "failed"
        assert row["result"]["feedback_status"] == expected
        assert row["result"]["elapsed_seconds"] >= 0
        assert all("feedback_seconds" not in level for level in row["result"]["levels"].values())
        assert all(level["feedback_status"] == expected for level in row["result"]["levels"].values())
        assert store.board()["participants"][0]["scores"]["1"] == 100
    else:
        assert row["status"] == ("time_limit" if failure == "timeout" else "system_error")
        assert row["score"] is None
        assert store.board()["participants"][0]["scores"]["1"] is None
    assert bool(killed) == (failure == "timeout")


def test_current_recipe_budget_and_scoring_policy_are_unchanged():
    settings = rules()
    assert settings["steps"] == 200 and settings["timeout_seconds"] == 600
    assert settings["implementation_points"] == 100 and settings["quality_points"] == 0
