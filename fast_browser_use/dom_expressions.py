"""Shared DOM/JS expressions and helpers used by every browser backend.

snapshot.js is the page-dict producer (READ_STATE); these are the smaller
expressions the driver interpolates node ids / action JSON into for
freshness guards and act dispatch. Both PlaywrightBrowser and EgoBrowser
import these so DOM semantics stay byte-identical across backends.
"""
import hashlib
import json
from pathlib import Path

READ_STATE = Path(__file__).with_name("snapshot.js").read_text()

# Cheap page-identity probe: returns the observed marker or None if the document navigated.
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"


def fingerprint(state):
    """sha256 over url/text/actions/scroll — order-independent (sort_keys)."""
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


_SELECT_ERRORS = {
    "not_confirmed": "Dropdown execution was not confirmed; inspect before retrying.",
    "interrupted": "Dropdown execution was interrupted; inspect before retrying.",
}


def select_runtime_error(kind):
    return RuntimeError(_SELECT_ERRORS[kind])
