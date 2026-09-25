
import pytest

from fast_browser_use.ego_browser import MAC_MODIFIERS, EgoBrowser, act_script, observe_script


def test_mac_modifiers():
    assert MAC_MODIFIERS("darwin") == 4
    assert MAC_MODIFIERS("linux") == 2
    assert MAC_MODIFIERS("win32") == 2


def test_observe_script_inlines_ids_and_uses_page_cdp():
    js = observe_script(7, "p1", "/p/snapshot.js")
    assert "taskSpace(7)" in js
    assert "task.page('p1')" in js
    assert "readFile('/p/snapshot.js'" in js
    assert "page.cdp('Runtime.evaluate'" in js
    assert "Page.captureScreenshot" in js


def test_act_script_fill_uses_keyboard_insertText_not_cdp():
    action = {"node": 2, "kind": "fill", "value": ""}
    js = act_script(7, "p1", action, "hello", "/p/snapshot.js", "darwin")
    assert "taskSpace(7)" in js
    assert "keyboard.insertText" in js  # NOT Input.insertText (POC #5)
    assert "Input.insertText" not in js
    assert "Input.dispatchMouseEvent" in js
    assert "modifiers: 4" in js  # mac selectAll
    assert "selectAll" in js


def test_act_script_click_has_no_text_insert():
    action = {"node": 3, "kind": "click", "value": ""}
    js = act_script(7, "p1", action, None, "/p/snapshot.js", "darwin")
    assert "Input.dispatchMouseEvent" in js
    assert "keyboard.insertText" not in js


def test_act_script_scroll_uses_mouseWheel_and_action_delta():
    action = {"node": 5, "kind": "scroll", "delta": 560}
    js = act_script(7, "p1", action, None, "/p/snapshot.js", "darwin")
    assert "mouseWheel" in js
    assert "action.delta" in js


def test_act_script_select_no_cdp_input_select_done_in_page_by_guard():
    action = {"node": 4, "kind": "select", "value": "opt1"}
    js = act_script(7, "p1", action, None, "/p/snapshot.js", "darwin")
    assert "Input.dispatchMouseEvent" not in js  # select set via in-page JS in the guard


def test_fresh_no_action_compares_marker(monkeypatch):
    b = EgoBrowser.__new__(EgoBrowser)
    b.space_id = 5
    b.page_label = "p1"
    b.server_name = None
    monkeypatch.setattr(b, "_run", lambda js: {"value": ["marker-x"]})
    assert b.fresh({"marker": ["marker-x"]}) is True
    assert b.fresh({"marker": ["other"]}) is False


def test_fresh_click_branch_compares_page_key_and_guard(monkeypatch):
    b = EgoBrowser.__new__(EgoBrowser)
    b.space_id = 5
    b.page_label = "p1"
    b.server_name = None
    monkeypatch.setattr(b, "_run", lambda js: {"value": ["pk", "g1"]})
    page = {"page_key": "pk", "guards": {"3": "g1"}}
    assert b.fresh(page, {"kind": "click", "node": 3}) is True


@pytest.mark.ego
def test_ego_observe_produces_contract_A_keys():
    b = EgoBrowser("file:///Users/mac/codes/fast-browser-use/benchmarks/pages/settings.html")
    page = b.observe()
    expected = {
        "url", "title", "language", "ready", "dialogs", "w", "h",
        "text", "scroll", "actions", "marker", "page_key", "guards", "omitted_actions",
    }
    assert expected <= set(page.keys())  # all 14 snapshot keys present (POC #4)
    assert "fingerprint" in page  # driver-added
    b.close()
