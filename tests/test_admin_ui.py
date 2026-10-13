"""Real-browser management flows against disposable CPU-only rehearsal states."""
import json
from html.parser import HTMLParser
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.request import urlopen

import pytest

from ai4sci_judge.store import Store
from judge_admin.auth import PasswordStore
from judge_admin.control import MemoryController
from judge_admin.demo import initialize_demo
from judge_admin.storage import AdminStorage
from judge_admin.web import create_server
from tests.test_judge_projector import Browser


TEST_PASSWORD = "isolated-browser-fixture-password-only-2026"


def test_login_cannot_send_a_password_in_the_url_before_javascript_loads():
    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.by_id = {}

        def handle_starttag(self, tag, attributes):
            attributes = dict(attributes)
            if "id" in attributes:
                self.by_id[attributes["id"]] = attributes

    document = Elements()
    document.feed((Path(__file__).resolve().parents[1] / "judge_admin/static/admin.html").read_text())
    assert document.by_id["login-form"]["method"] == "post"
    assert "name" not in document.by_id["password"]
    assert "disabled" in document.by_id["password"]
    assert "disabled" in document.by_id["login-submit"]
    assert document.by_id["password-form"]["method"] == "post"
    assert document.by_id["password-form"]["action"] == "/api/password/change"
    for field in ("current-password", "new-password", "confirm-password"):
        assert "name" not in document.by_id[field]
        assert "disabled" in document.by_id[field]
        assert document.by_id[field]["type"] == "password"
    assert document.by_id["current-password"]["autocomplete"] == "current-password"
    assert document.by_id["new-password"]["autocomplete"] == document.by_id["confirm-password"]["autocomplete"] == "new-password"


@pytest.fixture
def admin_screen(tmp_path):
    chromium = os.environ.get("AI4SCI_TEST_CHROMIUM")
    if not chromium or not Path(chromium).is_file():
        pytest.skip("Set AI4SCI_TEST_CHROMIUM for the optional administrator UI suite")
    websocket = pytest.importorskip("websocket")
    event_store = Store.initialize(tmp_path / "event", steps=2)
    demo_store = initialize_demo(tmp_path / "demo")
    live = AdminStorage(event_store.directory, tmp_path / "event-backups", MemoryController())
    demo = AdminStorage(demo_store.directory, tmp_path / "demo-backups", MemoryController())
    credentials = PasswordStore.initialize(tmp_path / "admin-password.json", TEST_PASSWORD)
    server = create_server(live, demo, None, 0, credential_store=credentials)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    profile = tmp_path / "chrome"
    process = subprocess.Popen([chromium, "--headless", "--no-sandbox", "--disable-gpu", "--no-proxy-server", "--remote-debugging-port=0", "--user-data-dir=" + str(profile), "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    socket = None
    try:
        active = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        while not active.exists():
            assert time.monotonic() < deadline, "Chromium startup timeout"
            time.sleep(.1)
        debug_port = int(active.read_text().splitlines()[0])
        # DevTools can accept requests before the initial about:blank target exists.
        page = None
        while page is None:
            with urlopen(f"http://127.0.0.1:{debug_port}/json", timeout=5) as response:
                page = next((item for item in json.load(response) if item["type"] == "page"), None)
            if page is None:
                assert time.monotonic() < deadline, "Chromium page startup timeout"
                time.sleep(.1)
        socket = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True, timeout=15)
        browser = Browser(socket)
        for domain in ("Runtime", "Page", "Network"):
            browser.call(domain + ".enable")
        browser.call("Page.addScriptToEvaluateOnNewDocument", {"source": "window.policyViolations=[];document.addEventListener('securitypolicyviolation',e=>policyViolations.push(e.violatedDirective));"})
        browser.call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        yield browser, live, demo, f"http://127.0.0.1:{server.server_port}"
        browser.js("true")
        assert not browser.exceptions
        assert browser.js("window.policyViolations") == []
    finally:
        if socket:
            socket.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def login(browser, url, path="/"):
    browser.call("Page.navigate", {"url": url + path})
    browser.until('document.body?.dataset.adminReady === "true"')
    browser.js('document.getElementById("password").value=' + json.dumps(TEST_PASSWORD) + ';document.getElementById("login-form").requestSubmit();true')
    browser.until('!document.getElementById("workspace").hidden && !busy')


def confirm(browser, text):
    browser.js('document.getElementById("confirm-text").value=' + json.dumps(text) + ';document.getElementById("confirm-text").dispatchEvent(new Event("input"));true')
    assert not browser.js('document.getElementById("confirm-submit").disabled')
    browser.js('document.getElementById("confirm-submit").click();true')
    browser.until('!busy && !document.getElementById("confirm-dialog").open')


def test_admin_login_session_and_responsive_live_status(admin_screen):
    browser, live, demo, url = admin_screen
    browser.call("Page.navigate", {"url": url})
    browser.until('document.body?.dataset.adminReady === "true"')
    browser.js('document.getElementById("password").value="wrong";document.getElementById("login-form").requestSubmit();true')
    browser.until('!document.getElementById("error").hidden && !busy')
    assert browser.js('document.getElementById("workspace").hidden')
    login(browser, url)
    assert browser.js('document.body.dataset.mode') == "live"
    assert browser.js('document.getElementById("participants").textContent') == "0"
    assert browser.js('document.getElementById("reset-event").disabled')
    assert browser.js('document.getElementById("password").value') == ""
    assert TEST_PASSWORD not in browser.js('location.href + document.documentElement.outerHTML')
    assert "ai4sci_admin" not in browser.js("document.cookie")
    assert browser.js('document.getElementById("projector-link").href') == "http://localhost:8091/display"
    assert browser.js('document.getElementById("demo-viewer").getAttribute("href")') == "/demo"
    for width, height in ((1440, 1000), (768, 1024)):
        browser.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        assert browser.js("document.documentElement.scrollWidth <= innerWidth"), width
        browser.screenshot(f"admin-live-{width}.png")
    browser.js('document.getElementById("logout").click();true')
    browser.until('document.getElementById("workspace").hidden && !busy')
    assert live.status()["participants"] == live.status()["submissions"] == 0
    assert demo.status()["participants"] == 30


def test_password_change_checks_current_password_and_requires_new_login(admin_screen):
    browser, live, demo, url = admin_screen
    replacement = "new-isolated-browser-password-2026"
    login(browser, url)
    assert not browser.js('document.getElementById("password-settings").hidden')
    browser.js('document.getElementById("password-settings").click();true')
    assert browser.js('document.activeElement.id') == "current-password"
    browser.js('window.passwordChangeRequests=0;window.originalFetch=window.fetch;window.fetch=(path,options)=>{if(path==="/api/password/change")window.passwordChangeRequests++;return window.originalFetch(path,options);};true')

    def fill(current, confirmation=replacement):
        values = {"current-password": current, "new-password": replacement, "confirm-password": confirmation}
        browser.js('Object.entries(' + json.dumps(values) + ').forEach(([id,value])=>document.getElementById(id).value=value);true')

    fill(TEST_PASSWORD, "does-not-match")
    browser.js('document.getElementById("password-form").requestSubmit();true')
    assert browser.js('document.getElementById("confirm-password").getAttribute("aria-invalid")') == "true"
    assert browser.js('window.passwordChangeRequests') == 0
    fill("incorrect-current-password")
    browser.js('document.getElementById("password-form").requestSubmit();true')
    browser.until('!busy && !document.getElementById("password-error").hidden')
    assert browser.js('document.getElementById("password-dialog").open && !document.getElementById("workspace").hidden')
    assert browser.js('document.getElementById("current-password").value') == ""

    fill(TEST_PASSWORD)
    browser.screenshot("admin-password-change.png")
    browser.js('document.getElementById("password-form").requestSubmit();document.getElementById("password-form").requestSubmit();true')
    browser.until('!busy && !document.getElementById("login-panel").hidden')
    assert browser.js('window.passwordChangeRequests') == 2  # One failed current-password check, one change.
    assert browser.js('document.getElementById("password-settings").hidden')
    assert not browser.js('document.getElementById("password-dialog").open')
    assert "새 비밀번호로 다시 로그인" in browser.js('document.getElementById("message").textContent')
    assert browser.js('["password","current-password","new-password","confirm-password"].every(id=>document.getElementById(id).value==="")')
    assert browser.js('localStorage.length + sessionStorage.length') == 0
    assert replacement not in browser.js('location.href + document.documentElement.outerHTML')
    browser.js('document.getElementById("password").value=' + json.dumps(TEST_PASSWORD) + ';document.getElementById("login-form").requestSubmit();true')
    browser.until('!busy && !document.getElementById("error").hidden')
    assert browser.js('document.getElementById("workspace").hidden')
    browser.js('document.getElementById("password").value=' + json.dumps(replacement) + ';document.getElementById("login-form").requestSubmit();true')
    browser.until('!busy && !document.getElementById("workspace").hidden')
    assert live.status()["participants"] == live.status()["submissions"] == 0
    assert demo.status()["participants"] == 30


def test_password_settings_cancel_demo_and_disabled_status(admin_screen):
    browser, live, demo, url = admin_screen
    login(browser, url)
    browser.js('document.getElementById("password-settings").click();document.getElementById("current-password").value="discard-me";document.getElementById("new-password").value="discard-this-too";document.getElementById("password-cancel").click();true')
    browser.until('!document.getElementById("password-dialog").open && document.getElementById("current-password").value===""')
    assert browser.js('document.getElementById("new-password").value') == ""
    browser.js('window.originalFetch=window.fetch;window.fetch=async(path,options)=>{const response=await window.originalFetch(path,options);if(String(path).endsWith("/status")&&response.ok){const status=await response.json();status.password_change_enabled=false;return new Response(JSON.stringify(status),{status:200});}return response;};document.getElementById("refresh").click();true')
    browser.until('document.getElementById("password-settings").hidden')
    assert browser.js('document.getElementById("password-submit").disabled')
    browser.call("Page.navigate", {"url": url + "/demo-admin"})
    browser.until('document.body?.dataset.adminReady === "true" && !document.getElementById("workspace").hidden')
    assert browser.js('document.getElementById("password-settings").hidden')
    assert browser.js('document.getElementById("password-submit").disabled')
    assert live.status()["participants"] == 0
    assert demo.status()["participants"] == 30


def test_password_change_partial_write_requires_immediate_reauthentication(admin_screen):
    browser, live, demo, url = admin_screen
    login(browser, url)
    browser.js('document.getElementById("password-settings").click();document.getElementById("current-password").value="current-fixture-password";document.getElementById("new-password").value="replacement-fixture-password";document.getElementById("confirm-password").value="replacement-fixture-password";window.originalFetch=window.fetch;window.fetch=(path,options)=>path==="/api/password/change"?Promise.resolve(new Response(JSON.stringify({reauthenticate:true,error:"저장 상태 확인이 필요합니다. 새 비밀번호로 다시 로그인해 주세요."}),{status:503})):window.originalFetch(path,options);document.getElementById("password-form").requestSubmit();true')
    browser.until('!busy && !document.getElementById("login-panel").hidden')
    assert browser.js('document.getElementById("workspace").hidden')
    assert not browser.js('document.getElementById("password-dialog").open')
    assert browser.js('currentStatus === null && csrfToken === ""')
    assert "새 비밀번호로 다시 로그인" in browser.js('document.getElementById("error").textContent')
    assert browser.js('["password","current-password","new-password","confirm-password"].every(id=>document.getElementById(id).value==="")')
    assert live.status()["participants"] == 0
    assert demo.status()["participants"] == 30


def test_demo_backup_reset_restore_and_full_reset_are_isolated(admin_screen):
    browser, live, demo, url = admin_screen
    live.store.add_participant("Isolated event account")
    original = demo.status()
    login(browser, url, "/demo-admin")
    assert browser.js('document.body.dataset.mode') == "demo"
    assert "GPU" in browser.js('document.getElementById("mode-description").textContent')
    assert browser.js('document.getElementById("projector-link").hidden')
    browser.js('document.getElementById("create-backup").click();true')
    browser.until('!busy && document.querySelectorAll("#backups tr").length === 1')
    backup_id = demo.backups()[0]["id"]
    assert browser.js('document.querySelector("[data-restore]").disabled')
    assert "/api/demo/backups/" in browser.js('document.querySelector("#backups a").getAttribute("href")')
    browser.js('document.getElementById("intake-toggle").click();true')
    browser.until('!busy && !document.getElementById("reset-scores").disabled')
    browser.js('document.getElementById("reset-scores").click();true')
    assert "유지" in browser.js('document.getElementById("confirm-scope").textContent')
    browser.js('document.getElementById("confirm-text").value="reset scores";document.getElementById("confirm-text").dispatchEvent(new Event("input"));true')
    assert browser.js('document.getElementById("confirm-submit").disabled')
    assert demo.status()["submissions"] == original["submissions"]
    confirm(browser, "RESET SCORES")
    assert demo.status()["submissions"] == 0
    assert demo.status()["participants"] == 30
    assert len(demo.backups()) >= 2
    browser.js('[...document.querySelectorAll("[data-restore]")].find(button=>button.dataset.restore===' + json.dumps(backup_id) + ').click();true')
    assert "접근 차단" in browser.js('document.getElementById("confirm-scope").textContent')
    confirm(browser, "RESTORE BACKUP")
    assert demo.status()["submissions"] == original["submissions"]
    browser.screenshot("admin-demo-restored-1440.png")
    browser.js('document.getElementById("reset-event").click();true')
    scope = browser.js('document.getElementById("confirm-scope").textContent')
    assert "기존 VM은 재연결" in scope and "접근 차단" in scope and "교체되지" in scope
    confirm(browser, "RESET EVENT")
    assert demo.status()["participants"] == demo.status()["submissions"] == 0
    assert "재연결" in browser.js('document.getElementById("message").textContent')
    assert live.status()["participants"] == 1
    assert live.status()["submissions"] == 0
    assert live.backups() == []


def test_queued_jobs_lock_reset_and_unrestorable_backups(admin_screen):
    browser, live, demo, url = admin_screen
    with demo.store.connect() as db:
        db.execute("UPDATE submissions SET status='queued',score=NULL WHERE id=(SELECT id FROM submissions LIMIT 1)")
    login(browser, url, "/demo-admin")
    browser.js('document.getElementById("intake-toggle").click();true')
    browser.until('!busy && currentStatus.maintenance')
    assert browser.js('document.getElementById("reset-scores").disabled')
    assert browser.js('document.getElementById("reset-event").disabled')
    browser.js('document.getElementById("create-backup").click();true')
    browser.until('!busy && document.querySelectorAll("#backups tr").length === 1')
    assert browser.js('document.querySelector("[data-restore]").disabled')
    assert demo.backups()[0]["compatible"] is False
    with demo.store.connect() as db:
        db.execute("UPDATE submissions SET status='completed',score=0 WHERE status='queued'")
    browser.js('document.getElementById("refresh").click();true')
    browser.until('!document.getElementById("reset-scores").disabled')
    assert browser.js('document.querySelector("[data-restore]").disabled')


def test_status_failure_locks_management_until_a_fresh_status(admin_screen):
    browser, live, demo, url = admin_screen
    demo.pause()
    login(browser, url, "/demo-admin")
    assert not browser.js('document.getElementById("reset-event").disabled')
    browser.js('window.originalFetch=window.fetch;window.fetch=(path,options)=>String(path).endsWith("/status")?Promise.resolve(new Response(JSON.stringify({error:"Synthetic offline status"}),{status:503})):window.originalFetch(path,options);document.getElementById("refresh").click();true')
    browser.until('document.getElementById("reset-event").disabled')
    assert browser.js('document.getElementById("create-backup").disabled')
    assert browser.js('document.getElementById("intake-toggle").disabled')
    browser.js('window.fetch=window.originalFetch;document.getElementById("refresh").click();true')
    browser.until('!document.getElementById("reset-event").disabled')
    assert demo.status()["participants"] == 30
    assert demo.backups() == []


def test_late_initial_unauthenticated_status_does_not_clear_new_login(admin_screen):
    browser, live, demo, url = admin_screen
    browser.call("Page.addScriptToEvaluateOnNewDocument", {"source": """
        window.originalFetch=window.fetch;
        window.fetch=(path,options)=>{
            if(String(path).endsWith('/status')&&!window.firstStatusHeld){
                window.firstStatusHeld=true;
                return new Promise(resolve=>window.releaseInitialStatus=()=>resolve(new Response(JSON.stringify({error:'Expired initial status'}),{status:401})));
            }
            return window.originalFetch(path,options);
        };
    """})
    login(browser, url, "/demo-admin")
    browser.until('typeof window.releaseInitialStatus === "function"')
    browser.js('window.releaseInitialStatus();true')
    time.sleep(.1)
    assert browser.js('document.getElementById("login-panel").hidden')
    assert browser.js('!document.getElementById("workspace").hidden && Boolean(csrfToken)')
    browser.js('document.getElementById("intake-toggle").click();true')
    browser.until('!busy && currentStatus?.maintenance === true')
    assert demo.status()["maintenance"]
    assert not live.status()["maintenance"]
