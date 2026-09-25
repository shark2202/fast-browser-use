import os
import sys

import pytest

import fast_browser_use.cli as cli
from fast_browser_use.browser import make_browser


def test_run_browser_flags_set_env(monkeypatch):
    monkeypatch.setattr(cli, "load_environment", lambda: None)
    # Stop right after env is set, before any model load / browser launch.
    monkeypatch.setattr("fast_browser_use.model.get_model", lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    leaked = ("FBU_BROWSER", "FBU_PROFILE_DIR", "FBU_GROUP", "FBU_HANDOFF", "FBU_HANDOFF_MODE")
    for v in leaked:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(
        sys, "argv",
        ["fbu", "run", "https://example.com", "--goal", "g",
         "--browser", "ego", "--profile-dir", "/p", "--group", "g1",
         "--handoff", "never", "--handoff-mode", "resume"],
    )
    try:
        with pytest.raises(RuntimeError, match="stop"):
            cli.main()
        assert os.environ["FBU_BROWSER"] == "ego"
        assert os.environ["FBU_PROFILE_DIR"] == "/p"
        assert os.environ["FBU_GROUP"] == "g1"
        assert os.environ["FBU_HANDOFF"] == "never"
        assert os.environ["FBU_HANDOFF_MODE"] == "resume"
    finally:
        # cli.main() mutates os.environ directly (not tracked by monkeypatch); clean up to avoid leaking
        # FBU_BROWSER=ego into later tests (which would make Agent construct EgoBrowser, lacking .locale/.headless).
        for v in leaked:
            os.environ.pop(v, None)


def test_ego_preflight_rejects_non_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ValueError, match="macOS"):
        make_browser("https://example.com", browser="ego")


def test_ego_preflight_missing_binary(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ValueError, match="ego-browser"):
        make_browser("https://example.com", browser="ego")
