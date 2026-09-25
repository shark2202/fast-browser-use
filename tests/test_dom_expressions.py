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
