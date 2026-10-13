"""Overall results are opt-in; the projector never submits or authenticates."""
import json
import time
from urllib.parse import urlsplit

import pytest

from tests.test_judge_projector import screen, synthetic_board


def choose(browser, view):
    browser.js(f'document.getElementById("ranking").value={json.dumps(view)}; document.getElementById("ranking").dispatchEvent(new Event("change")); true')


def ready(browser, view):
    columns = 7 if view == "overall" else 3
    browser.until(f'document.body.dataset.connection === "live" && document.body.dataset.ranking === {json.dumps(view)} && document.querySelectorAll("thead th").length === {columns}')


def assert_read_only(browser):
    browser.js("true")  # Drain request events before checking the captured methods.
    assert browser.request_methods
    assert set(browser.request_methods) == {"GET"}
    assert not any("/api/me" in request or "/api/submissions" in request for request in browser.requests)


def test_overall_bookmark_uses_server_ranks_totals_ties_and_missing_scores(screen):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    data.payload["participants"] = [
        {"nickname": "No score", "scores": dict.fromkeys("1234"), "total": 0, "rank": None, "challenge_ranks": dict.fromkeys("1234")},
        {"nickname": "Zero score", "scores": {"1": None, "2": 0, "3": None, "4": None}, "total": 0, "rank": 4, "challenge_ranks": {"1": None, "2": 2, "3": None, "4": None}},
        {"nickname": "Beta tie", "scores": dict.fromkeys("1234", 25), "total": 100, "rank": 1, "challenge_ranks": {"1": 3, "2": 1, "3": 1, "4": 1}},
        {"nickname": "Alpha tie", "scores": {"1": 100, "2": None, "3": None, "4": None}, "total": 100, "rank": 1, "challenge_ranks": {"1": 1, "2": None, "3": None, "4": None}},
        {"nickname": "Partial score", "scores": {"1": 60, "2": None, "3": None, "4": None}, "total": 60, "rank": 3, "challenge_ranks": {"1": 2, "2": None, "3": None, "4": None}},
    ]
    browser.open(url + "/display?ranking=overall&rotate=0")
    ready(browser, "overall")
    assert browser.js('document.getElementById("ranking").value') == "overall"
    assert "overall" in browser.js('document.getElementById("challenge-title").textContent').lower()
    headers = browser.js('[...document.querySelectorAll("thead th")].map(e=>e.textContent)')
    assert all(label in header for label, header in zip(("Rank", "Participant", "C1", "C2", "C3", "C4", "Total"), headers))
    assert browser.js('[...document.querySelectorAll("#standings tr")].map(row=>[...row.cells].map(cell=>cell.textContent))') == [
        ["1", "Alpha tie", "100.00", "-", "-", "-", "100.00"],
        ["1", "Beta tie", "25.00", "25.00", "25.00", "25.00", "100.00"],
        ["3", "Partial score", "60.00", "-", "-", "-", "60.00"],
        ["4", "Zero score", "-", "0.00", "-", "-", "0.00"],
        ["-", "No score", "-", "-", "-", "-", "-"],
    ]
    assert browser.js('document.getElementById("stat-running").textContent') == "8"
    assert browser.js('document.getElementById("stat-completed").textContent') == "278"
    requests = [urlsplit(request) for request in browser.requests if urlsplit(request).path == "/api/board"]
    assert requests and all(request.query == "" for request in requests)
    # Rendering must use authoritative server values, not recompute ranks or totals.
    data.payload["participants"] = [data.payload["participants"][4]]
    data.payload["participants"][0].update(rank=7, total=59.99)
    browser.js("refreshBoard()")
    assert browser.js('[...document.querySelector("#standings tr").cells].map(cell=>cell.textContent)') == ["7", "Partial score", "60.00", "-", "-", "-", "59.99"]
    assert_read_only(browser)


def test_overall_is_opt_in_and_switching_back_restores_challenge_only_data(screen):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    browser.open(url + "/display?rotate=0")
    ready(browser, "1")
    assert all("?challenge=1" in request for request in browser.requests if "/api/board" in request)
    browser.js('document.getElementById("next-page").click()')
    assert browser.js("page") == 1
    choose(browser, "overall")
    ready(browser, "overall")
    assert browser.js("page") == 0
    assert browser.js('location.search.includes("ranking=overall")')
    assert browser.js('document.querySelector("#standings tr").cells.length') == 7
    assert any(urlsplit(request).path == "/api/board" and not urlsplit(request).query for request in browser.requests)
    browser.js('document.getElementById("next-page").click()')
    assert browser.js("page") == 1
    choose(browser, "3")
    ready(browser, "3")
    assert browser.js("page") == 0
    assert browser.js('document.querySelector("#standings tr").cells.length') == 3
    assert browser.js('Object.keys(board.participants[0]).sort()') == ["challenge_ranks", "nickname", "scores"]
    assert browser.js('Object.keys(board.participants[0].scores)') == ["3"]
    assert browser.js('document.getElementById("stat-running").textContent') == "2"
    assert browser.js('document.getElementById("stat-completed").textContent') == "63"
    assert_read_only(browser)


def test_empty_overall_results_have_seven_column_placeholder(screen):
    browser, data, url = screen
    browser.open(url + "/display?ranking=overall&rotate=0")
    ready(browser, "overall")
    assert browser.js('document.querySelector("#standings .empty").colSpan') == 7
    assert browser.js('document.getElementById("page-label").textContent') == "Page 1 / 1"
    assert browser.js('document.getElementById("previous-page").disabled')
    assert browser.js('document.getElementById("next-page").disabled')
    assert_read_only(browser)


def test_overall_paging_stays_in_view_and_pauses_on_disconnect(screen):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    browser.open(url + "/display?ranking=overall&rotate=0")
    ready(browser, "overall")
    browser.js('document.getElementById("next-page").click()')
    assert browser.js("page") == 1
    browser.js('document.getElementById("rotate-pages").click(); nextRotation=Date.now()-1;')
    browser.until("page === 2")
    assert browser.js('document.getElementById("ranking").value') == "overall"
    assert browser.js('document.querySelectorAll("thead th").length') == 7
    browser.call("Network.emulateNetworkConditions", {"offline": True, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0})
    browser.js("refreshBoard()")
    browser.until('document.body.dataset.connection === "stale"')
    browser.js("page=0; drawBoard(); nextRotation=Date.now()-1;")
    previous = browser.js('document.querySelector("#standings tr").textContent')
    time.sleep(1.2)
    assert browser.js("page") == 0
    assert browser.js('document.querySelector("#standings tr").textContent') == previous
    assert browser.js('document.getElementById("stale-warning").hidden') is False
    browser.call("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
    browser.js("refreshBoard()")
    ready(browser, "overall")
    assert browser.js('document.getElementById("stale-warning").hidden') is True
    assert_read_only(browser)


@pytest.mark.parametrize(("old_view", "new_view"), [("overall", "3"), ("3", "overall")])
def test_offline_view_switch_never_displays_previous_results(screen, old_view, new_view):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    browser.open(url + f"/display?ranking={old_view}&rotate=0")
    ready(browser, old_view)
    browser.call("Network.emulateNetworkConditions", {"offline": True, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0})
    choose(browser, new_view)
    browser.until('document.body.dataset.connection === "stale"')
    assert browser.js('document.querySelectorAll("#standings .rank").length') == 0
    assert browser.js('document.querySelector("#standings .empty").colSpan') == (7 if new_view == "overall" else 3)
    assert browser.js('document.getElementById("stat-completed").textContent') == "-"
    browser.call("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
    browser.js("refreshBoard()")
    ready(browser, new_view)
    assert_read_only(browser)


@pytest.mark.parametrize(("old_view", "new_view"), [("overall", "3"), ("3", "overall")])
def test_late_response_from_previous_view_is_not_rendered(screen, old_view, new_view):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    browser.open(url + "/display?ranking=1&rotate=0")
    old_path = "/api/board" if old_view == "overall" else f"/api/board?challenge={old_view}"
    browser.js("""window.originalFetch = window.fetch;
        window.delayedPath = """ + json.dumps(old_path) + """;
        window.fetch = (input, options) => {
            if (input === window.delayedPath) {
                window.delayedPath = null;
                return new Promise(resolve => {
                    window.releaseOldResponse = () => { window.originalFetch(input, options).then(resolve); };
                });
            }
            return window.originalFetch(input, options);
        }; true""")
    choose(browser, old_view)
    browser.until('typeof window.releaseOldResponse === "function"')
    choose(browser, new_view)
    assert browser.js('document.querySelectorAll("#standings .rank").length') == 0
    assert browser.js('document.querySelector("#standings .empty").colSpan') == (7 if new_view == "overall" else 3)
    browser.js("window.releaseOldResponse(); true")
    ready(browser, new_view)
    assert browser.js('document.querySelector("#standings tr").cells.length') == (7 if new_view == "overall" else 3)
    if new_view != "overall":
        assert browser.js('Object.keys(board.participants[0].scores)') == [new_view]
    assert_read_only(browser)


def test_overall_projector_readability_and_mobile_table_scrolling(screen):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    browser.open(url + "/display?ranking=overall&rotate=0")
    ready(browser, "overall")
    # Synthetic markup-like nicknames remain text, never an image or script.
    assert browser.js('document.querySelectorAll("#standings img").length') == 0
    assert browser.js('document.getElementById("standings").textContent.includes("<img src=x onerror=alert(1)>")')
    data.payload["participants"][0]["nickname"] = "Practice learner 001"
    data.payload["participants"][1]["nickname"] = "Practice learner 002"
    browser.js("refreshBoard()")
    for width, height in ((1920, 1080), (1512, 982), (1440, 900), (1366, 768), (1280, 720), (390, 844)):
        browser.size(width, height)
        assert browser.js("document.documentElement.scrollWidth <= innerWidth"), (width, height)
        assert browser.js('document.querySelectorAll("thead th").length') == 7
        assert browser.js('document.querySelectorAll("#standings tr").length') == 10
        if width >= 1280:
            assert browser.js("document.documentElement.scrollHeight <= innerHeight"), (width, height)
            assert browser.js('parseFloat(getComputedStyle(document.querySelector("td")).fontSize) >= 21')
        else:
            assert browser.js('document.querySelector(".table-scroll").scrollWidth > document.querySelector(".table-scroll").clientWidth')
            assert browser.js('getComputedStyle(document.querySelector(".table-scroll")).overflowX') in {"auto", "scroll"}
        browser.screenshot(f"overall-projector-{width}.png")
    assert_read_only(browser)
