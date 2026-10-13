"""Paging and score feedback in a real browser, using disposable score fixtures."""
import pytest

from tests.test_judge_projector import screen, synthetic_board


def open_board(screen, view="1", count=23, reduced=False):
    browser, data, url = screen
    data.payload = synthetic_board(data.board())
    data.payload["participants"] = data.payload["participants"][:count]
    for index, row in enumerate(data.payload["participants"], 1):
        row["nickname"] = f"Motion practice {index:03}"
    browser.call("Emulation.setEmulatedMedia", {"features": [
        {"name": "prefers-reduced-motion", "value": "reduce" if reduced else "no-preference"},
    ]})
    browser.call("Page.addScriptToEvaluateOnNewDocument", {"source": """
        window.policyViolations=[];
        document.addEventListener('securitypolicyviolation', e=>policyViolations.push(e.violatedDirective));
    """})
    browser.open(url + f"/display?ranking={view}&rotate=0")
    browser.until('document.body.dataset.font === "nvidia"')
    assert browser.js('document.getAnimations().length') == 0
    return browser, data


def cells(browser):
    return browser.js('''[...document.querySelectorAll('#standings tr')].map(row=>[...row.cells].map(cell=>cell.textContent))''')


def animated_cells(browser):
    return browser.js('''document.getAnimations().filter(a=>a.effect.target.matches('#standings td'))
        .map(a=>({name:a.effect.target.parentElement.cells[1].textContent,index:a.effect.target.cellIndex}))''')


def settled(browser):
    browser.until('document.getAnimations().length === 0', timeout=3)
    assert browser.js('window.policyViolations') == []
    assert browser.js('document.querySelectorAll("table").length') == 1
    assert browser.js('document.querySelectorAll("#standings").length') == 1


@pytest.mark.parametrize("view", ["1", "overall"])
def test_page_slides_immediately_updates_rows_and_keeps_chrome_fixed(screen, view):
    browser, data = open_board(screen, view)
    for width, height in ((1920, 1080), (1280, 720), (390, 844)):
        browser.size(width, height)
        # Device metrics dispatch resize asynchronously; let the real resize handler finish.
        browser.js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(true))))')
        browser.js('page=0;drawBoard();')
        before = browser.js('''[document.querySelector('thead'),document.querySelector('.board-footer')]
            .map(e=>({top:e.getBoundingClientRect().top,left:e.getBoundingClientRect().left}))''')
        immediate = browser.js('''document.getElementById('next-page').click();
            window.motion=document.getAnimations().find(a=>a.effect.target.id==='standings');
            motion.pause();motion.currentTime=120;
            ({page,rows:document.querySelectorAll('#standings tr').length,
              label:document.getElementById('page-label').textContent,
              animations:document.getAnimations().filter(a=>a.effect.target.id==='standings').length})''')
        assert immediate == {"page": 1, "rows": 10, "label": "Page 2 / 3", "animations": 1}
        assert not animated_cells(browser)
        # Freeze a real in-flight frame rather than testing only the final class.
        after = browser.js('''[document.querySelector('thead'),document.querySelector('.board-footer')]
            .map(e=>({top:e.getBoundingClientRect().top,left:e.getBoundingClientRect().left}))''')
        assert after == before
        assert browser.js('document.documentElement.scrollWidth <= innerWidth')
        if width >= 1280:
            assert browser.js('document.documentElement.scrollHeight <= innerHeight')
        if view == "overall" and width == 390:
            assert browser.js('document.querySelector(".table-scroll").scrollWidth > document.querySelector(".table-scroll").clientWidth')
        forward = browser.js('new DOMMatrixReadOnly(getComputedStyle(document.getElementById("standings")).transform).m41')
        assert forward > 0
        browser.screenshot(f"slide-{view}-{width}.png")
        browser.js('motion.play();true')
        settled(browser)
        browser.js('document.getElementById("next-page").click();document.getElementById("next-page").click();')
        settled(browser)
        assert len(cells(browser)) == 3
        footer_top = browser.js('document.querySelector(".board-footer").getBoundingClientRect().top')
        assert abs(footer_top - before[1]["top"]) < 1, (width, height, footer_top, before)
        backward = browser.js('''document.getElementById('previous-page').click();
            window.motion=document.getAnimations().find(a=>a.effect.target.id==='standings');
            motion.pause();motion.currentTime=120;
            new DOMMatrixReadOnly(getComputedStyle(document.getElementById('standings')).transform).m41''')
        assert backward < 0
        browser.js('motion.play();true')
        settled(browser)


def test_partial_page_fast_navigation_poll_and_view_switch_cannot_restore_old_rows(screen):
    browser, _ = open_board(screen)
    browser.js('document.getElementById("next-page").click();document.getElementById("next-page").click();')
    assert browser.js('page') == 2
    assert len(cells(browser)) == 3
    assert {row[1] for row in cells(browser)} == {f"Motion practice {n:03}" for n in (21, 22, 23)}
    browser.js('refreshBoard()')
    assert browser.js('page') == 2
    assert len(cells(browser)) == 3
    settled(browser)
    browser.js('document.getElementById("previous-page").click();document.getElementById("next-page").click();document.getElementById("previous-page").click();')
    assert browser.js('page') == 1
    settled(browser)
    assert [row[1] for row in cells(browser)] == [f"Motion practice {n:03}" for n in range(11, 21)]
    browser.js('document.getElementById("next-page").click();document.getElementById("ranking").value="overall";document.getElementById("ranking").dispatchEvent(new Event("change"));')
    browser.until('board && !board.challenge && document.body.dataset.connection === "live"')
    assert browser.js('page') == 0
    assert browser.js('document.getAnimations().length') == 0
    assert all(len(row) == 7 for row in cells(browser))
    settled(browser)


def test_auto_wrap_and_keyboard_share_paging_motion_without_challenge_rotation(screen):
    browser, _ = open_board(screen)
    browser.js('document.body.dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowRight",bubbles:true}));')
    assert browser.js('page') == 1
    assert browser.js('document.getAnimations().some(a=>a.effect.target.id === "standings")')
    settled(browser)
    browser.js('page=pageCount-1;drawBoard();document.getElementById("rotate-pages").click();nextRotation=Date.now()-1;')
    browser.until('page === 0')
    assert browser.js('document.getAnimations().some(a=>a.effect.target.id === "standings")')
    assert browser.js('document.getElementById("ranking").value') == "1"
    assert browser.js('rotationMs') == 5000
    assert len(cells(browser)) == 10
    browser.js('document.getElementById("rotate-pages").click()')
    settled(browser)


@pytest.mark.parametrize("view,indexes", [("1", [2]), ("overall", [2, 6])])
def test_only_real_changed_scores_flash_and_final_numbers_are_immediate(screen, view, indexes):
    browser, data = open_board(screen, view)
    browser.js('refreshBoard()')
    assert browser.js('document.getAnimations().length') == 0
    data.payload["participants"][0]["scores"]["1"] = 99.25
    data.payload["participants"][0]["total"] += 1.25
    browser.js('refreshBoard()')
    assert sorted(animated_cells(browser), key=lambda cell: cell["index"]) == [
        {"name": "Motion practice 001", "index": index} for index in indexes
    ]
    assert not browser.js('document.getAnimations().some(a=>a.effect.target.id === "standings")')
    assert cells(browser)[0][2] == "99.25"
    browser.screenshot(f"score-update-{view}.png")
    settled(browser)
    for redraw in ('refreshBoard()', 'drawBoard()', 'window.dispatchEvent(new Event("resize"))'):
        browser.js(redraw)
        assert browser.js('document.getAnimations().length') == 0
    browser.js('document.getElementById("next-page").click()')
    assert not animated_cells(browser)
    settled(browser)


def test_first_zero_score_and_offscreen_score_do_not_flash_other_participants(screen):
    browser, data = open_board(screen)
    data.payload["participants"][5]["scores"]["1"] = None
    browser.js('refreshBoard()')
    settled(browser)
    data.payload["participants"][5]["scores"]["1"] = 0.0
    data.payload["participants"][20]["scores"]["1"] = 81.2
    browser.js('refreshBoard()')
    assert animated_cells(browser) == [{"name": "Motion practice 006", "index": 2}]
    assert cells(browser)[5][2] == "0.00"
    settled(browser)
    browser.js('document.getElementById("next-page").click();document.getElementById("next-page").click();')
    assert not animated_cells(browser)
    settled(browser)


def test_reduced_motion_disables_both_effects_including_live_preference_changes(screen):
    browser, data = open_board(screen, reduced=True)
    browser.js('document.getElementById("next-page").click()')
    assert browser.js('page') == 1
    assert browser.js('document.getAnimations().length') == 0
    data.payload["participants"][10]["scores"]["1"] += 0.5
    browser.js('refreshBoard()')
    assert browser.js('document.getAnimations().length') == 0
    browser.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "no-preference"}]})
    browser.js('document.getElementById("previous-page").click()')
    assert browser.js('document.getAnimations().length') > 0
    browser.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
    browser.until('document.getAnimations().length === 0')
    assert browser.js('page') == 0
    settled(browser)


def test_overall_first_zero_score_also_highlights_new_zero_total(screen):
    browser, data = open_board(screen, view="overall", count=5)
    participant = data.payload["participants"][0]
    participant["scores"] = dict.fromkeys("1234")
    participant["total"] = 0
    participant["rank"] = None
    browser.js('refreshBoard()')
    settled(browser)
    participant["scores"]["1"] = 0.0
    participant["rank"] = 5
    browser.js('refreshBoard()')
    assert sorted(animated_cells(browser), key=lambda cell: cell["index"]) == [
        {"name": "Motion practice 001", "index": 2},
        {"name": "Motion practice 001", "index": 6},
    ]
    row = next(row for row in cells(browser) if row[1] == "Motion practice 001")
    assert row[2] == row[6] == "0.00"
    settled(browser)


def test_real_score_poll_keeps_an_inflight_page_slide_and_latest_values(screen):
    browser, data = open_board(screen)
    browser.js('''document.getElementById('next-page').click();
        window.slideBefore=document.getAnimations().find(a=>a.effect.target.id==='standings');
        slideBefore.pause();slideBefore.currentTime=120;true''')
    data.payload["participants"][10]["scores"]["1"] = 95.75
    browser.js('refreshBoard()')
    assert browser.js('document.getAnimations().find(a=>a.effect.target.id === "standings") === slideBefore')
    assert browser.js('slideBefore.currentTime') == 120
    assert cells(browser)[0][2] == "95.75"
    assert animated_cells(browser) == [{"name": "Motion practice 011", "index": 2}]
    browser.js('slideBefore.play();true')
    settled(browser)


def test_nickname_only_change_is_not_a_score_update(screen):
    browser, data = open_board(screen, view="overall")
    data.payload["participants"][0]["nickname"] = "Updated practice nickname"
    browser.js('refreshBoard()')
    assert browser.js('document.getAnimations().length') == 0
    assert any(row[1] == "Updated practice nickname" for row in cells(browser))


def test_focused_overall_scroll_region_retains_arrow_keys(screen):
    browser, _ = open_board(screen, view="overall")
    browser.size(390, 844)
    result = browser.js('''const region=document.querySelector('.table-scroll');region.focus();
        const arrow=new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true});
        region.dispatchEvent(arrow);({page,prevented:arrow.defaultPrevented})''')
    assert result == {"page": 0, "prevented": False}
