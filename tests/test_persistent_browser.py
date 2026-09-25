import pytest

from fast_browser_use.browser import PlaywrightBrowser


@pytest.mark.persistent
def test_persistent_mode_reuses_login_state_across_instances(tmp_path):
    pytest.importorskip("playwright")
    d = str(tmp_path / "profile")
    b1 = PlaywrightBrowser("https://example.com", profile_dir=d)
    b1.call("Runtime.evaluate", expression="localStorage.setItem('fbu','ok')", returnByValue=True)
    assert b1.session_id == d
    b1.close()
    # Relaunch with the same profile dir: state persisted on disk (POC #7).
    b2 = PlaywrightBrowser("https://example.com", profile_dir=d)
    val = b2.call("Runtime.evaluate", expression="localStorage.getItem('fbu')", returnByValue=True)["result"]["value"]
    assert val == "ok"
    b2.close()
