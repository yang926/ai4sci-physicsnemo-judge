"""Separate repository, read-only viewer and package-boundary regressions."""
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ai4sci_judge.catalog import PROJECT_ROOT, fingerprint, rules
from ai4sci_judge.store import Store
from ai4sci_judge.web import create_server


def test_state_cannot_be_inside_judge_repository():
    with pytest.raises(ValueError, match="outside"):
        Store.initialize(PROJECT_ROOT / "private-state")


def test_missing_course_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr("ai4sci_judge.catalog.ROOT", tmp_path / "missing")
    with pytest.raises(ValueError, match="submodule"):
        fingerprint(rules(2))


def test_projector_listener_has_no_account_or_submission_routes(tmp_path):
    store = Store.initialize(tmp_path / "state", steps=2)
    token = store.add_participant("Display fixture")
    server = create_server(store, 0, display_only=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for route in ("/", "/display", "/display/"):
            with urlopen(base + route) as response:
                html = response.read().decode()
            assert 'data-view="display"' in html and 'id="token"' not in html
        with urlopen(base + "/api/board") as response:
            payload = json.load(response)
        assert payload["participants"][0]["nickname"] == "Display fixture"
        assert token not in json.dumps(payload)
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + "/api/me", headers={"Authorization": "Bearer " + token}))
        assert error.value.code == 404
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + "/api/submissions", data=b'{}', headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}))
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
