"""Challenge lifecycle controls and projector states on disposable local data."""
import re

import pytest

from tests.test_admin_ui import admin_screen, login
from tests.test_judge_projector import screen, synthetic_board


def button(key, action):
    return f'document.querySelector("[data-challenge=\\"{key}\\"][data-challenge-action=\\"{action}\\"]")'


def test_admin_close_and_finalize_require_separate_confirmation(admin_screen):
    browser, live, demo, url = admin_screen
    login(browser, url, "/demo-admin")
    assert browser.js('document.querySelectorAll(".challenge-control").length') == 4
    assert browser.js(button("1", "finalize") + '.disabled')
    browser.js(button("1", "schedule_close") + '.click();true')
    browser.until('!busy && currentStatus.lifecycle["1"].state === "closing"')
    assert browser.js(button("1", "schedule_close") + '.disabled')
    assert "남음" in browser.js('document.getElementById("challenge-1-timing").textContent')
    browser.js(button("1", "close") + '.click();true')
    browser.until('!busy && currentStatus.lifecycle["1"].state === "closed"')
    assert browser.js(button("1", "close") + '.disabled')
    with demo.store.connect() as db:
        db.execute("UPDATE submissions SET status='system_error',score=NULL WHERE id=(SELECT id FROM submissions WHERE challenge='1' LIMIT 1)")
    browser.js('refreshStatus()')
    assert browser.js(button("1", "finalize") + '.disabled')
    browser.js(button("1", "retry_errors") + '.click();true')
    browser.until('!busy && currentStatus.lifecycle["1"].queued === 1 && currentStatus.lifecycle["1"].system_error === 0')
    assert browser.js(button("1", "finalize") + '.disabled')
    with demo.store.connect() as db:
        db.execute("UPDATE submissions SET status='completed',score=0 WHERE challenge='1' AND status='queued'")
    browser.js('refreshStatus()')
    browser.js(button("1", "finalize") + '.click();true')
    assert browser.js('document.getElementById("finalize-dialog").open')
    assert not browser.js('document.getElementById("confirm-dialog").open')
    assert "정답" in browser.js('document.getElementById("finalize-intro").textContent')
    assert browser.js('document.activeElement.id') == "finalize-cancel"
    browser.screenshot("lifecycle-admin-finalize-confirm.png")
    browser.js('document.getElementById("finalize-cancel").click();true')
    assert demo.status()["lifecycle"]["1"]["state"] == "closed"
    browser.js(button("1", "finalize") + '.click();document.getElementById("finalize-submit").click();true')
    browser.until('!busy && currentStatus.lifecycle["1"].state === "finalized"')
    assert browser.js('document.getElementById("finalize-dialog").open') is False
    assert browser.js('Array.from(document.querySelectorAll("[data-challenge=\\"1\\"]")).every(b=>b.disabled)')
    assert live.status()["lifecycle"]["1"]["state"] == "open"
    assert demo.status()["lifecycle"]["2"]["state"] == "open"
    browser.js('document.getElementById("challenges-title").scrollIntoView({block:"start"});')
    browser.screenshot("lifecycle-admin-controls.png")
    for width in (1440, 768, 390):
        browser.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        assert browser.js('document.documentElement.scrollWidth <= innerWidth')
    browser.js('document.getElementById("challenges-title").scrollIntoView({block:"start"});')
    browser.screenshot("lifecycle-admin-mobile.png")


def test_admin_clock_busy_errors_stale_and_legacy_status_lock_finalization(admin_screen):
    browser, _, _, url = admin_screen
    login(browser, url)
    browser.js('''currentStatus.lifecycle["1"] = {state:"closing",deadline:1300,finalized_at:null,queued:0,running:0,system_error:0};
        lifecycleClock = {serverTime:1000,received:performance.now()};renderLifecycles();updateControls();''')
    assert browser.js('document.getElementById("challenge-1-timing").textContent') == "05:00 남음"
    browser.js('lifecycleClock.serverTime = 1301;renderLifecycles();updateControls();')
    assert "상태 확인 중" in browser.js('document.getElementById("challenge-1-timing").textContent')
    assert browser.js(button("1", "finalize") + '.disabled')
    for field in ("queued", "running", "system_error"):
        browser.js('''currentStatus.lifecycle["1"] = {state:"closed",deadline:1000,finalized_at:null,queued:0,running:0,system_error:0};
            currentStatus.lifecycle["1"]["''' + field + '''"] = 1;renderLifecycles();updateControls();''')
        assert browser.js(button("1", "finalize") + '.disabled')
    assert not browser.js(button("1", "retry_errors") + '.hidden')
    assert not browser.js(button("1", "retry_errors") + '.disabled')
    browser.js('lifecycleClock.received -= 26000;renderLifecycles();updateControls();')
    assert browser.js(button("1", "retry_errors") + '.disabled')
    assert browser.js('document.getElementById("challenge-1-state").textContent') == "상태 확인 필요"
    browser.js('delete currentStatus.lifecycle;renderLifecycles();updateControls();')
    assert browser.js('Array.from(document.querySelectorAll("[data-challenge-action]")).every(b=>b.disabled)')
    browser.js('renderAudit(Array.from({length:15},(_,i)=>({action:"challenge_action_completed",challenge:"1",transition:"retry_errors",retried:15-i})));')
    assert "시스템 오류 재채점 · 완료 (15건)" in browser.js('document.querySelector("#audit li").textContent')


@pytest.mark.parametrize("view", ["1", "overall"])
def test_projector_clock_final_status_staleness_and_layout(screen, view):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    data.payload["participants"][0]["nickname"] = "Practice learner 001"
    data.payload["participants"][1]["nickname"] = "Practice learner 002"
    data.payload["server_time"] = 1000
    data.payload["lifecycle"] = {key: {"state": "closing", "deadline": 1300, "finalized_at": None, "queued": 0, "running": 0, "system_error": 0} for key in "1234"}
    for queue in data.payload["queue_by_challenge"].values():
        queue.update(queued=0, running=0)
    data.payload["queue"] = {field: sum(queue[field] for queue in data.payload["queue_by_challenge"].values()) for field in ("completed", "queued", "running")}
    browser.open(url + f"/display?ranking={view}&rotate=0")
    assert browser.js('document.querySelector("#lifecycle-1 .lifecycle-countdown").textContent') == "05:00 remaining"
    assert browser.js('document.querySelectorAll(".lifecycle-item").length') == (4 if view == "overall" else 1)
    browser.js('lifecycleClock.serverTime=1301;updateLifecycle();')
    assert "Checking status" in browser.js('document.getElementById("lifecycle-1").textContent')
    assert "Final results" not in browser.js('document.getElementById("lifecycle-strip").textContent')
    for state, counts, expected in [
        ("closed", {"queued": 1}, "Submissions closed · Grading"),
        ("closed", {"queued": 0}, "Closed · Awaiting finalization"),
        ("finalized", {}, "Final results"),
    ]:
        data.payload["lifecycle"]["1"].update(state=state, **counts)
        data.payload["queue_by_challenge"]["1"].update(counts)
        data.payload["queue"]["queued"] = sum(queue["queued"] for queue in data.payload["queue_by_challenge"].values())
        browser.js('refreshBoard()')
        assert browser.js('document.querySelector("#lifecycle-1 .lifecycle-state").textContent') == expected
    browser.js('connectionFailed=true;updateConnection();')
    assert "Last known status" in browser.js('document.getElementById("lifecycle-1").textContent')
    assert browser.js('document.getElementById("lifecycle-1").dataset.state') == "unknown"
    assert "Status unverified" in browser.js('document.getElementById("result-stage").textContent')
    browser.js('refreshBoard()')
    assert browser.js('document.getElementById("stat-running").textContent') == "0"
    assert browser.js('document.getElementById("stat-queued").textContent') == "0"
    for width, height in ((1920, 1080), (1280, 720), (390, 844)):
        browser.size(width, height)
        browser.screenshot(f"lifecycle-projector-{view}-{width}.png")
        assert browser.js('document.documentElement.scrollWidth <= innerWidth')
        if width >= 1280:
            assert browser.js('document.documentElement.scrollHeight <= innerHeight'), (width, height, browser.js('document.documentElement.scrollHeight'))
        assert browser.js('document.querySelectorAll("#standings tr").length') == 10
    assert browser.js('rotationMs') == 5000


def assert_projection_labels_are_english(browser):
    """Check app-owned copy and accessible labels, not attendees' chosen names."""
    owned_copy = browser.js('''(() => {
        const content = document.body.cloneNode(true);
        content.querySelectorAll('#standings tr td:nth-child(2), script, style').forEach(node => node.remove());
        return content.textContent + [...content.querySelectorAll('[aria-label], [title]')]
            .map(node => (node.getAttribute('aria-label') || '') + (node.getAttribute('title') || '')).join(' ');
    })()''')
    assert not re.search(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", owned_copy), owned_copy


@pytest.mark.parametrize("view", ["1", "overall"])
def test_projection_lifecycle_copy_is_english_but_nicknames_are_preserved(screen, view):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    data.payload["participants"][0]["nickname"] = "김연습"
    data.payload["participants"][1]["nickname"] = "Practice learner 002"
    data.payload["server_time"] = 1000
    data.payload["lifecycle"] = {key: {"state": "open", "deadline": None, "finalized_at": None,
                                              "queued": 0, "running": 0, "system_error": 0} for key in "1234"}
    browser.open(url + f"/display?ranking={view}&rotate=0")
    assert browser.js('document.documentElement.lang') == "en"
    assert browser.js('document.getElementById("lifecycle-strip").getAttribute("aria-label")') == "Challenge submission and result status"
    browser.size(1280, 720)

    cases = [
        ("open", None, 1000, {}, "Submissions open", ""),
        ("closing", 1300, 1000, {}, "Closing soon", "05:00 remaining"),
        ("closing", 1000, 1001, {}, "Closing soon", "Checking status" if view == "overall" else "Deadline reached · Checking status"),
        ("closing", 1300, None, {}, "Closing soon", "Server time unavailable"),
        ("closed", 1000, 1001, {"queued": 1}, "Submissions closed · Grading", ""),
        ("closed", 1000, 1001, {"running": 1}, "Submissions closed · Grading", ""),
        ("closed", 1000, 1001, {"system_error": 1}, "Submissions closed · Reviewing errors", ""),
        ("closed", 1000, 1001, {}, "Closed · Awaiting finalization", ""),
        ("unrecognized", None, 1001, {}, "Checking status", ""),
        ("finalized", 1000, 1001, {}, "Final results", ""),
    ]
    for state, deadline, server_time, counts, label, countdown in cases:
        data.payload["server_time"] = server_time
        for lifecycle in data.payload["lifecycle"].values():
            lifecycle.update(state=state, deadline=deadline, queued=0, running=0, system_error=0)
            lifecycle.update(counts)
        browser.js('refreshBoard()')
        assert browser.js('document.querySelector("#lifecycle-1 .lifecycle-state").textContent') == label
        assert browser.js('document.querySelector("#lifecycle-1 .lifecycle-countdown").textContent') == countdown
        assert browser.js('document.getElementById("result-stage").textContent') == ("Final results confirmed" if state == "finalized" else "Provisional results")
        assert "김연습" in browser.js('document.getElementById("standings").textContent')
        assert_projection_labels_are_english(browser)
        assert browser.js('document.documentElement.scrollWidth <= innerWidth'), (view, state, label)
        if browser.js('document.documentElement.scrollHeight > innerHeight'):
            browser.screenshot(f"english-projector-{view}-overflow.png")
        assert browser.js('document.documentElement.scrollHeight <= innerHeight'), (view, state, label, countdown, browser.js('document.documentElement.scrollHeight'))
        if label == "Closed · Awaiting finalization":
            browser.screenshot(f"english-projector-{view}-awaiting-finalization-1280.png")
        if countdown == "Server time unavailable":
            browser.screenshot(f"english-projector-{view}-unavailable-clock-1280.png")

    browser.call("Network.emulateNetworkConditions", {"offline": True, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0})
    browser.js('refreshBoard()')
    browser.until('document.body.dataset.connection === "stale"')
    assert browser.js('document.querySelector("#lifecycle-1 .lifecycle-state").textContent') == "Last known status: Final results · Reconnecting"
    assert browser.js('document.getElementById("result-stage").textContent') == "Last received results · Status unverified"
    assert not browser.js('document.getElementById("stale-warning").hidden')
    assert_projection_labels_are_english(browser)
    browser.screenshot(f"english-projector-{view}-stale.png")
    browser.call("Network.emulateNetworkConditions", {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1})
    browser.js('refreshBoard()')
    browser.until('document.body.dataset.connection === "live"')
    assert browser.js('document.querySelector("#lifecycle-1 .lifecycle-state").textContent') == "Final results"
    assert browser.js('document.getElementById("stale-warning").hidden')
    assert "김연습" in browser.js('document.getElementById("standings").textContent')
    assert_projection_labels_are_english(browser)
    browser.screenshot(f"english-projector-{view}-final.png")


def test_demo_projection_is_english_and_administrator_screen_stays_korean(admin_screen):
    browser, _, _, url = admin_screen
    login(browser, url)
    assert browser.js('document.getElementById("workspace-title").textContent') == "채점 현황"
    browser.open(url + "/demo?ranking=1&rotate=0")
    assert browser.js('document.querySelector(".demo-banner").textContent').startswith("DEMO")
    assert_projection_labels_are_english(browser)
    browser.screenshot("english-demo-projector.png")
    browser.call("Page.navigate", {"url": url + "/demo-admin"})
    browser.until('document.body?.dataset.adminReady === "true" && !document.getElementById("workspace").hidden')
    assert browser.js('document.documentElement.lang') == "ko"
    assert browser.js('document.getElementById("workspace-title").textContent') == "채점 현황"
    assert browser.js('document.getElementById("challenges-title").textContent') == "Challenge별 마감과 결과 확정"


def test_clock_only_polls_do_not_interrupt_score_feedback_and_legacy_is_neutral(screen):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    browser.open(url + "/display?ranking=overall&rotate=0")
    data.payload["participants"][0]["scores"]["1"] += 0.75
    browser.js('refreshBoard()')
    browser.js('window.pulse=document.getAnimations().find(a=>a.effect.target.matches("td"));pulse.pause();pulse.currentTime=120;true')
    data.payload["server_time"] += 1
    browser.js('refreshBoard()')
    assert browser.js('document.getAnimations().includes(pulse)')
    browser.js('pulse.finish();true')
    # A legacy response must never imply that the event is open or finalized.
    data.payload.pop("lifecycle", None)
    data.payload.pop("server_time", None)
    browser.js('refreshBoard()')
    assert browser.js('document.getElementById("lifecycle-strip").hidden')
    assert browser.js('document.querySelectorAll("#standings tr").length') == 10
