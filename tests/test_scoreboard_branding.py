"""Event branding stays self-contained, accessible, and legible on a projector."""
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import pytest

from tests.test_judge_projector import screen, synthetic_board


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "ai4sci_judge" / "static"
SVG_SOURCE = ROOT / "course" / "ETC" / "assets" / "branding" / "nvidia-logo-horz.svg"


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        for attribute in ("src", "poster", "xlink:href"):
            if attribute in attributes:
                self.urls.append(attributes[attribute])
        if tag == "link" and attributes.get("rel") in {"stylesheet", "icon", "preload"}:
            self.urls.append(attributes.get("href", ""))


def test_nvidia_logo_preserves_official_geometry_and_accessible_name():
    html = (STATIC / "display.html").read_text()
    candidates = re.findall(r"<svg\b.*?</svg>", html, flags=re.S)
    logos = [ET.fromstring(svg) for svg in candidates if "nvidia-logo" in svg]
    assert len(logos) == 1
    logo = logos[0]
    original = ET.parse(SVG_SOURCE).getroot()
    assert "nvidia-logo" in logo.attrib.get("class", "").split()
    assert logo.attrib.get("role") == "img"
    assert logo.attrib.get("aria-label") == "NVIDIA"
    assert logo.attrib.get("aria-hidden") != "true"
    assert logo.attrib.get("viewBox") == original.attrib["viewBox"] == "0 0 2560 1440"
    assert logo.attrib.get("preserveAspectRatio", "xMidYMid meet") != "none"

    def paths(svg):
        return [node.attrib for node in svg.iter() if node.tag.rsplit("}", 1)[-1] == "path"]

    assert paths(logo) == paths(original), "Do not redraw or distort the official NVIDIA mark"
    assert all(node.tag.rsplit("}", 1)[-1] != "style" for node in logo.iter())
    assert re.search(r"\.nvidia-logo\s+\.cls-1\s*\{[^}]*fill\s*:\s*#76b900\s*[;}]", (STATIC / "style.css").read_text(), re.I)


def test_event_branding_needs_no_external_assets_or_placeholder_mark():
    html = (STATIC / "display.html").read_text()
    assets = Assets()
    assets.feed(html)
    assert assets.urls
    assert all(not urlsplit(url).netloc and urlsplit(url).scheme in {"", "data"} for url in assets.urls)
    css = (STATIC / "style.css").read_text()
    assert not re.search(r"url\(\s*['\"]?(?:https?:)?//", css, re.I)
    assert not re.search(r"@import\s+['\"](?:https?:)?//", css, re.I)
    assert "brand-mark" not in html, "Replace the AI placeholder with the event's supplied branding"
    assert "AI4Science Korea 2026" in html
    assert "NVIDIA PhysicsNeMo Tutorial" in html
    assert any(date in html for date in ("2026-09-30", "2026.09.30", "September 30, 2026", "30 September 2026"))


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "ranked"])
def test_branding_layout_and_logo_visibility_across_screen_sizes(screen, populated):
    browser, data, url = screen
    if populated:
        data.payload = synthetic_board(data.board())
    browser.open(url + "/display?rotate=0")
    for width, height in ((1920, 1080), (1366, 768), (390, 844), (320, 740)):
        browser.size(width, height)
        assert browser.js("document.documentElement.scrollWidth <= innerWidth"), (width, height)
        if width >= 1366:
            assert browser.js("document.documentElement.scrollHeight <= innerHeight"), (width, height)
        dimensions = browser.js("""(() => {
            const svg = document.querySelector('svg.nvidia-logo');
            if (!svg) return null;
            const paths = [...svg.querySelectorAll('path')].map(path => path.getBoundingClientRect());
            const matrix = svg.getScreenCTM();
            return {
                label: svg.getAttribute('aria-label'),
                visibility: getComputedStyle(svg).visibility,
                display: getComputedStyle(svg).display,
                left: Math.min(...paths.map(rect => rect.left)),
                right: Math.max(...paths.map(rect => rect.right)),
                top: Math.min(...paths.map(rect => rect.top)),
                bottom: Math.max(...paths.map(rect => rect.bottom)),
                scaleX: Math.hypot(matrix.a, matrix.b),
                scaleY: Math.hypot(matrix.c, matrix.d),
                eyeFill: getComputedStyle(svg.querySelector('#Eye_Mark')).fill,
                wordFill: getComputedStyle(svg.querySelector('#NVIDIA')).fill
            };
        })()""")
        assert dimensions is not None
        assert dimensions["label"] == "NVIDIA"
        assert dimensions["visibility"] == "visible"
        assert dimensions["display"] != "none"
        assert dimensions["right"] - dimensions["left"] >= 120, (width, dimensions)
        assert 0 <= dimensions["left"] < dimensions["right"] <= width
        assert 0 <= dimensions["top"] < dimensions["bottom"] <= height
        assert dimensions["scaleX"] > 0
        assert dimensions["scaleX"] == pytest.approx(dimensions["scaleY"], rel=.001)
        assert dimensions["eyeFill"] == "rgb(118, 185, 0)"
        assert dimensions["wordFill"] == "rgb(0, 0, 0)"
        browser.screenshot(f"branding-{'ranked' if populated else 'empty'}-{width}.png")
    assert all(urlsplit(request).netloc == urlsplit(url).netloc for request in browser.requests if urlsplit(request).scheme in {"http", "https"})
