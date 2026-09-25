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


@pytest.mark.persistent
def test_persistent_handoff_resume_roundtrip(tmp_path):
    pytest.importorskip("playwright")
    d = str(tmp_path / "prof2")
    b = PlaywrightBrowser("https://example.com", profile_dir=d, handoff_mode="resume")
    rec = b.handoff("login required")
    assert rec["mechanism"] == "resume"
    assert rec["reason"] == "login required"
    assert rec["url"].startswith("https://example.com")
    b.close()  # context already closed by handoff(); no-op
    b2 = PlaywrightBrowser("https://example.com", profile_dir=d)  # resume: same profile dir
    page = b2.takeover()
    assert "url" in page and "actions" in page
    b2.close()


def test_isolated_resume_raises():
    # No browser launched: the resume-without-profile_dir check raises before touching the driver.
    b = PlaywrightBrowser.__new__(PlaywrightBrowser)
    b.profile_dir = None
    b.handoff_mode = "auto"
    with pytest.raises(ValueError, match="isolated context cannot survive process exit"):
        b.handoff("x", mechanism="resume")
