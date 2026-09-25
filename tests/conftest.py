import os

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "persistent: needs FBU_PERSISTENT_LIVE=1 + playwright browsers")
    config.addinivalue_line("markers", "ego: needs FBU_EGO_LIVE=1 + running ego lite + Full Access")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "persistent" in item.keywords and os.environ.get("FBU_PERSISTENT_LIVE") != "1":
            item.add_marker(pytest.mark.skip(reason="set FBU_PERSISTENT_LIVE=1"))
        if "ego" in item.keywords and os.environ.get("FBU_EGO_LIVE") != "1":
            item.add_marker(pytest.mark.skip(reason="set FBU_EGO_LIVE=1"))
