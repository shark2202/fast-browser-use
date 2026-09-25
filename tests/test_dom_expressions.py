import json

from fast_browser_use import dom_expressions


def test_marker_is_the_snapshot_marker_probe():
    # MARKER wraps READ_STATE so a stale page returns None marker without re-running the full scan.
    assert isinstance(dom_expressions.MARKER, str)
    assert "marker" in dom_expressions.MARKER
    assert dom_expressions.MARKER.startswith("(() => {")


def test_fingerprint_is_stable_and_order_independent():
    state = {"url": "u", "text": "t", "actions": [{"id": "e1"}], "scroll": {"y": 0, "height": 100}}
    a = dom_expressions.fingerprint(state)
    other = {"scroll": {"height": 100, "y": 0}, "actions": [{"id": "e1"}], "url": "u", "text": "t"}
    b = dom_expressions.fingerprint(other)
    assert a == b and len(a) == 64  # sha256 hex


def test_select_runtime_error_messages():
    not_confirmed = str(dom_expressions.select_runtime_error("not_confirmed"))
    interrupted = str(dom_expressions.select_runtime_error("interrupted"))
    assert not_confirmed == "Dropdown execution was not confirmed; inspect before retrying."
    assert interrupted == "Dropdown execution was interrupted; inspect before retrying."


def test_scroll_fresh_guard_inlines_node():
    expr = dom_expressions.scroll_fresh_guard(7)
    assert "c.pageKey()" in expr
    assert "c.scrollGuard(c.nodes.get(7))" in expr
    assert expr.startswith("(() => {")


def test_click_fresh_guard_inlines_node():
    expr = dom_expressions.click_fresh_guard(3)
    assert "c.pageKey()" in expr
    assert "c.guard(c.nodes.get(3))" in expr


def test_scroll_point_inlines_node_or_scrolling_element():
    assert "c.nodes.get(5)" in dom_expressions.scroll_point(5)
    assert "document.scrollingElement" in dom_expressions.scroll_point(None)


def test_act_dispatch_guard_inlines_action_json():
    action_json = '{"node": 2, "kind": "fill"}'
    expr = dom_expressions.act_dispatch_guard(action_json)
    assert "window.__fastBrowserUse" in expr
    assert action_json in expr
    assert expr.startswith("(action => {")


def test_after_input_wait_is_a_promise_expression():
    payload = json.dumps({"node": 1, "kind": "fill"})
    expr = dom_expressions.after_input_wait(payload)
    assert "new Promise" in expr
    assert "resolve" in expr
    assert expr.endswith(payload + ")")
