"""Untouched NVIDIA font assets render locally without weakening the projector."""
import base64
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit

import pytest

from tests.test_judge_projector import screen, synthetic_board


STATIC = Path(__file__).resolve().parents[1] / "ai4sci_judge" / "static"
ORIGINAL_FONTS = {
    400: (17528, "da76017f2084c111962999f89ae2398f14db97e8689d09da3e90bdbac501c088"),
    500: (17516, "63a4b0f9afa94218a92a9afbc09da3af6a01b713d416007cf079264794aeff3d"),
    700: (17084, "e77a40c0c57903432aabc67c176e9ad9ac6768acc1883a3a14557aa2f2f8184a"),
}


class FontData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attributes = []
        self.chunks = []
        self.inside = False

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "script" and attributes.get("id") == "brand-fonts":
            self.attributes.append(attributes)
            self.inside = True

    def handle_data(self, text):
        if self.inside:
            self.chunks.append(text)

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False


def test_embedded_woff2_fonts_are_untouched_official_originals():
    parser = FontData()
    parser.feed((STATIC / "display.html").read_text())
    assert len(parser.attributes) == 1
    assert parser.attributes[0].get("type") == "application/json"
    assert "src" not in parser.attributes[0]
    payload = json.loads("".join(parser.chunks))
    assert len(payload) == 3
    assert {entry["weight"] for entry in payload} == set(ORIGINAL_FONTS)
    for entry in payload:
        font = base64.b64decode(entry["data"], validate=True)
        length, digest = ORIGINAL_FONTS[entry["weight"]]
        assert font.startswith(b"wOF2")
        assert len(font) == length
        assert hashlib.sha256(font).hexdigest() == digest


def observe_policy(browser, *, font_failure=False):
    source = """window.fontPolicyViolations = [];
        document.addEventListener('securitypolicyviolation', event => {
            window.fontPolicyViolations.push({directive: event.violatedDirective, blocked: event.blockedURI});
        });"""
    if font_failure:
        source += "window.FontFace = function () { throw new Error('Synthetic font loading failure'); };"
    browser.call("Page.addScriptToEvaluateOnNewDocument", {"source": source})


def platform_fonts(browser, selector):
    browser.call("DOM.enable")
    browser.call("CSS.enable")
    root = browser.call("DOM.getDocument")["root"]["nodeId"]
    node = browser.call("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
    assert node, selector
    return browser.call("CSS.getPlatformFontsForNode", {"nodeId": node})["fonts"]


def assert_local_and_policy_safe(browser, url):
    browser.js("true")
    assert browser.js("window.fontPolicyViolations") == []
    requests = [urlsplit(request) for request in browser.requests]
    assert all(request.netloc == urlsplit(url).netloc for request in requests if request.scheme in {"http", "https"})
    assert not any(request.path.lower().endswith((".woff", ".woff2", ".ttf", ".otf")) for request in requests)
    assert set(browser.request_methods) == {"GET"}


@pytest.mark.parametrize("view", ["3", "overall"], ids=["challenge", "overall"])
def test_nvidia_sans_is_the_actual_rendered_font_and_layout_fits(screen, view):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    data.payload["participants"][0]["nickname"] = "Practice learner 001"
    data.payload["participants"][1]["nickname"] = "Practice learner 002"
    observe_policy(browser)
    browser.open(url + f"/display?ranking={view}&rotate=0")
    browser.until('document.body.dataset.font === "nvidia"')
    browser.js("document.fonts.ready.then(() => true)")
    assert browser.js("""[...document.fonts]
        .filter(font => font.family.replaceAll('"', '') === 'NVIDIA Sans')
        .map(font => ({weight: font.weight, status: font.status}))
        .sort((a,b) => Number(a.weight)-Number(b.weight))""") == [
        {"weight": "400", "status": "loaded"},
        {"weight": "500", "status": "loaded"},
        {"weight": "700", "status": "loaded"},
    ]
    family = browser.js("getComputedStyle(document.body).fontFamily.split(',')[0]")
    assert family.strip(' "\'') == "NVIDIA Sans"
    for selector in ("#challenge-title", "#standings tr:first-child td:last-child"):
        actual = platform_fonts(browser, selector)
        assert any(font["isCustomFont"] and "nvidia" in font["familyName"].lower() and font["glyphCount"] > 0 for font in actual), actual
    for width, height in ((1920, 1080), (1366, 768), (390, 844), (320, 740)):
        browser.size(width, height)
        assert browser.js("document.documentElement.scrollWidth <= innerWidth"), (view, width, height)
        assert browser.js('document.querySelectorAll("thead th").length') == (7 if view == "overall" else 3)
        if width >= 1366:
            assert browser.js("document.documentElement.scrollHeight <= innerHeight"), (view, width, height)
            assert browser.js('parseFloat(getComputedStyle(document.querySelector("td")).fontSize) >= 21')
        browser.screenshot(f"nvidia-sans-{view}-{width}.png")
    assert_local_and_policy_safe(browser, url)


@pytest.mark.parametrize("view", ["3", "overall"], ids=["challenge", "overall"])
def test_font_load_failure_keeps_the_scoreboard_live_using_fallback(screen, view):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    observe_policy(browser, font_failure=True)
    browser.open(url + f"/display?ranking={view}&rotate=0")
    browser.until('document.body.dataset.font === "fallback"')
    assert browser.js('document.body.dataset.connection') == "live"
    assert browser.js('document.getElementById("ranking").value') == view
    assert browser.js('document.querySelector("#standings tr").cells.length') == (7 if view == "overall" else 3)
    assert browser.js('document.querySelectorAll("#standings .rank").length') > 0
    actual = platform_fonts(browser, "#challenge-title")
    assert actual and all(not font["isCustomFont"] for font in actual), actual
    browser.size(390, 844)
    assert browser.js("document.documentElement.scrollWidth <= innerWidth")
    assert_local_and_policy_safe(browser, url)


def test_hangul_nickname_remains_unchanged_with_nvidia_sans(screen):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    data.payload["participants"] = [data.payload["participants"][0]]
    data.payload["participants"][0]["nickname"] = "한글닉네임 연습"
    browser.open(url + "/display?rotate=0")
    browser.until('document.body.dataset.font === "nvidia"')
    assert browser.js('document.querySelector("#standings tr td:nth-child(2)").textContent') == "한글닉네임 연습"
    browser.size(320, 740)
    assert browser.js("document.documentElement.scrollWidth <= innerWidth")


def test_hangul_fallback_stack_and_installed_system_glyphs(screen):
    fontconfig = shutil.which("fc-list")
    available = subprocess.run([fontconfig, ":lang=ko", "family"], check=True, capture_output=True, text=True).stdout.strip() if fontconfig else ""
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    data.payload["participants"] = [data.payload["participants"][0]]
    data.payload["participants"][0]["nickname"] = "한글닉네임"
    browser.open(url + "/display?rotate=0")
    browser.until('document.body.dataset.font === "nvidia"')
    browser.js("document.fonts.ready.then(() => true)")
    families = browser.js('getComputedStyle(document.querySelector("#standings tr td:nth-child(2)")).fontFamily')
    assert "Apple SD Gothic Neo" in families
    assert "Malgun Gothic" in families
    assert families.rstrip().endswith("sans-serif")
    # Verify actual Hangul glyph rendering only on hosts that have a Hangul
    # font. A Linux host without one still checks the configured macOS/Windows
    # fallback stack above; this is not evidence that Korean glyphs rendered.
    if available:
        actual = platform_fonts(browser, "#standings tr td:nth-child(2)")
        assert any(not font["isCustomFont"] and font["glyphCount"] > 0 and "nvidia" not in font["familyName"].lower() for font in actual), actual
