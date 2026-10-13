"""Required notebook-to-service rehearsal, including real CPU workers.

The pinned course supplies the notebook client by default. AI4SCI_NOTEBOOK_ROOT
can select a compatibility checkout; grading still uses the trusted course.
"""
import inspect
import json
import os
from pathlib import Path
import threading
from urllib.request import urlopen

from ai4sci_judge.catalog import CHALLENGES, ROOT
from ai4sci_judge.store import Store
from ai4sci_judge.web import create_server
from ai4sci_judge.worker import work_once
from tests.test_judge import answer
from tests.test_judge_operators import operator_answer


def test_one_notebook_identity_submits_all_eleven_levels_to_separate_service(tmp_path, monkeypatch):
    root = Path(os.environ.get("AI4SCI_NOTEBOOK_ROOT", ROOT)).resolve()
    assert (root / "ETC/runtime/judge_client.py").is_file(), "Pinned course must include the student notebook client"
    monkeypatch.syspath_prepend(str(root))
    from ETC.runtime.judge_client import JudgeClient
    from ETC.runtime.submission import collect_submission
    for function, filename in ((JudgeClient, "judge_client.py"), (collect_submission, "submission.py")):
        assert Path(inspect.getfile(function)).resolve() == root / "ETC/runtime" / filename, (
            "Notebook compatibility test imported a different course checkout; "
            "run this test alone when overriding AI4SCI_NOTEBOOK_ROOT"
        )

    # Never let the embedded course service accidentally satisfy this test.
    assert create_server.__module__ == "ai4sci_judge.web"
    assert work_once.__module__ == "ai4sci_judge.worker"
    store = Store.initialize(tmp_path / "private", steps=2)
    token = store.add_participant()
    participant_id = store.authenticate(token)["id"]
    other = store.add_participant("Other fixture")
    lesson = tmp_path / "exercise"
    lesson.mkdir()
    server = create_server(store, 0)
    display = create_server(store, 0, display_only=True)
    threads = [threading.Thread(target=service.serve_forever, daemon=True) for service in (server, display)]
    for thread in threads:
        thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        client = JudgeClient(url, token)
        assert client.me()["nickname"] is None
        assert client.me()["submission_contract"] == 3
        assert client.register_nickname("Notebook bridge fixture")["nickname"] == "Notebook bridge fixture"
        receipts = []
        for challenge, spec in CHALLENGES.items():
            for level, filename in enumerate(spec["files"], 1):
                source = operator_answer(level) if challenge == "4" else answer(challenge, filename)
                (lesson / filename).write_text(source)
            payload = collect_submission(challenge, lesson, levels=tuple(range(1, len(spec["files"]) + 1)))
            receipt = client.submit(payload)
            receipts.append(receipt)
            assert client.me()["submissions"][0]["id"] == receipt
            assert work_once(store, device="cpu")
            account = client.me()
            history = account["submissions"]
            assert account["nickname"] == "Notebook bridge fixture"
            assert history[0]["status"] == "completed", history[0]
            assert history[0]["score"] == 100, (challenge, history[0])
            levels = history[0]["result"]["levels"]
            assert set(levels) == set(spec["files"])
            assert all(value["status"] == "evaluated" for value in levels.values())
            assert JudgeClient(url, other).me()["submissions"] == []
            with urlopen(f"http://127.0.0.1:{display.server_port}/api/board?challenge={challenge}") as response:
                board = json.load(response)
            assert board["challenge"] == challenge
            assert set(board["challenges"]) == {challenge}
            assert board["participants"][0]["scores"] == {challenge: 100}
            assert board["participants"][0]["nickname"] == "Notebook bridge fixture"
            assert set(board["participants"][0]) == {"nickname", "scores", "challenge_ranks"}
            assert token not in json.dumps(board)
            assert client.submit(payload) == receipt
        assert len(receipts) == 4
        assert len(client.me()["submissions"]) == 4
        assert store.authenticate(token)["id"] == participant_id
        renamed = client.register_nickname("Updated notebook name")
        assert {item["id"] for item in renamed["submissions"]} == set(receipts)
        assert store.board()["participants"][0]["total"] == 400
    finally:
        for service in (server, display):
            service.shutdown()
            service.server_close()
        for thread in threads:
            thread.join(timeout=5)
