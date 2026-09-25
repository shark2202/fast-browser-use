import os

import pytest

from fast_browser_use.browser import PlaywrightBrowser, default_storage_path


def _default_storage_path(group):
    """Mirror the default path the browser uses for a per-group storage_state file."""
    return os.path.expanduser(f"~/.fbu/storage/{group}.json")


@pytest.mark.persistent
def test_isolated_storage_state_per_group_roundtrip(tmp_path):
    pytest.importorskip("playwright")
    g = str(tmp_path / "gA.json")
    b1 = PlaywrightBrowser("https://example.com", group="gA", storage_state_path=g)
    b1.call("Runtime.evaluate", expression="localStorage.setItem('grp','A')", returnByValue=True)
    b1.close()  # saves storage_state to g on close
    assert os.path.exists(g)
    b2 = PlaywrightBrowser("https://example.com", group="gA", storage_state_path=g)  # loads g
    val = b2.call("Runtime.evaluate", expression="localStorage.getItem('grp')", returnByValue=True)["result"]["value"]
    assert val == "A"
    b2.close()


def test_default_storage_path_per_group():
    assert default_storage_path("team-x") == os.path.expanduser("~/.fbu/storage/team-x.json")
    assert default_storage_path("a-b_C.0") == os.path.expanduser("~/.fbu/storage/a-b_C.0.json")
