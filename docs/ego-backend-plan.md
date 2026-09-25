# Browser-Backend Abstraction & Two-Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `fbu` a pluggable browser layer with two backends — `playwright` (cross-platform, isolated/persistent modes) and `ego` (macOS-only) — covering login reuse, per-group isolation, and human–AI handoff.

**Architecture:** Extract the shared DOM/JS expressions and `fingerprint()` into `dom_expressions.py`; rename `browser.py`'s `Browser` → `PlaywrightBrowser` implementing a `Browser` Protocol behind a `make_browser()` factory; add `--profile-dir` (persistent mode) and `--group` (per-group `storage_state`/profile dir) and `--handoff`/`--handoff-mode` (pause/resume) on Playwright; add `EgoBrowser` (macOS-only) driving `ego-browser nodejs` via `page.cdp()`/`page.evaluate()` with a `keyboard.insertText` substitution. `agent.py` call sites unchanged.

**Tech Stack:** Python 3.12+, Playwright sync API, `playwright.sync_api.sync_playwright`; ego via external `ego-browser` CLI subprocess; tests via `pytest` (gated live marks for persistent/ego).

**Spec:** `docs/ego-backend-design.md` (contracts A/B, §4.4 shared JS, §5 isolation, §6 resume, §7 handoff, §10 errors). Evidence: `docs/ego-backend-poc-findings.md`.

## Global Constraints (from spec §5 Constraints + §13 Compatibility)

- Zero-hallucination preserved: the model only chooses among observed DOM elements; handoff is harness-triggered, never model-generated.
- Guarded execution preserved: `StalePage`, freshness guards, DONE-as-gate, `verification.py` run identically on both backends/modes.
- Local-first: no cloud inference; both backends run locally.
- No black-box IPC: ego driven only via documented `ego-browser nodejs` CLI + `page.cdp()`/`page.evaluate()`.
- Profile safety: persistent mode uses a per-group dir or a copy, never the user's live Chrome profile (Chromium locks it).
- Python ≥ 3.12; `pyproject.toml` unchanged (no new deps for playwright paths; ego needs only the external `ego-browser` CLI).
- Default `FBU_BROWSER=playwright` (isolated, no `--profile-dir`, no `--group`, no `--handoff`) is byte-equivalent to `main` (zero regression).
- Platform honesty: `ego` is macOS-only; preflight rejects it elsewhere.

---

## File Structure

**New files:**
- `fast_browser_use/dom_expressions.py` — shared JS expression constants (`MARKER`, `after_input`, `scroll_guard`, `click_guard`, `scroll_point`, `act_dispatch_guard`) + `fingerprint()` + `select_runtime_error()` mapping. Imported by both backends.
- `fast_browser_use/ego_browser.py` — `EgoBrowser` (macOS-only): subprocess driver + JS templates + `keyboard.insertText` swap + native handOff/takeover.
- `fast_browser_use/handoff.py` — `detect_handoff(page, history) -> reason|None` heuristic detector (login/captcha/2FA/BLOCKED).
- `tests/test_dom_expressions.py`, `tests/test_persistent_browser.py`, `tests/test_browser_isolation.py`, `tests/test_ego_backend.py`, `tests/test_handoff.py`.

**Modified files:**
- `fast_browser_use/browser.py` — rename `Browser`→`PlaywrightBrowser`; consume `dom_expressions`; add `profile_dir`/`storage_state`/`handoff`/`takeover` (isolated+persistent modes). Keep `call`/`evaluate`/`browser_operation` private.
- `fast_browser_use/agent.py` — construct via `make_browser()`; handle `handoff` status; record `state["handoffs"]`.
- `fast_browser_use/cli.py` — `--browser`, `--profile-dir`, `--group`, `--handoff`, `--handoff-mode`, `resume` subcommand, preflight.
- `fast_browser_use/__init__.py` — export `make_browser`.

**Phases:** P1 abstraction refactor (zero behavior change) → P2 persistent mode + resume → P3 isolated `storage_state` + in-process pause → P4 `EgoBrowser` → P5 CLI + handoff detector → P6 docs/SKILL.

---

# Phase 1: Abstraction Refactor (zero behavior change)

Foundation: extract shared JS, rename class, add Protocol + factory. All existing tests stay green.

### Task 1.1: Extract `dom_expressions.py` (MARKER + fingerprint + select error)

**Files:**
- Create: `fast_browser_use/dom_expressions.py`
- Modify: `fast_browser_use/browser.py` (consume the new module)
- Test: `tests/test_dom_expressions.py`

**Interfaces:**
- Produces: `dom_expressions.MARKER` (str), `dom_expressions.fingerprint(state: dict) -> str`, `dom_expressions.select_runtime_error(kind: str) -> RuntimeError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dom_expressions.py
from fast_browser_use import dom_expressions


def test_marker_is_the_snapshot_marker_probe():
    # MARKER wraps READ_STATE so a stale page returns None marker without re-running the full scan.
    assert isinstance(dom_expressions.MARKER, str)
    assert "marker" in dom_expressions.MARKER
    assert dom_expressions.MARKER.startswith("(() => {")


def test_fingerprint_is_stable_and_order_independent():
    state = {"url": "u", "text": "t", "actions": [{"id": "e1"}], "scroll": {"y": 0, "height": 100}}
    a = dom_expressions.fingerprint(state)
    b = dom_expressions.fingerprint({"scroll": {"height": 100, "y": 0}, "actions": [{"id": "e1"}], "url": "u", "text": "t"})
    assert a == b and len(a) == 64  # sha256 hex


def test_select_runtime_error_messages():
    assert str(dom_expressions.select_runtime_error("not_confirmed")) == "Dropdown execution was not confirmed; inspect before retry."
    assert str(dom_expressions.select_runtime_error("interrupted")) == "Dropdown execution was interrupted; inspect before retry."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dom_expressions.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'fast_browser_use.dom_expressions'`

- [ ] **Step 3: Write minimal implementation**

```python
# fast_browser_use/dom_expressions.py
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
    "not_confirmed": "Dropdown execution was not confirmed; inspect before retry.",
    "interrupted": "Dropdown execution was interrupted; inspect before retry.",
}


def select_runtime_error(kind):
    return RuntimeError(_SELECT_ERRORS[kind])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_dom_expressions.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Wire `browser.py` to use the module (no behavior change)**

In `fast_browser_use/browser.py`: replace the local `READ_STATE`/`MARKER`/`fingerprint` definitions with imports, keeping `browser_operation`/`fingerprint` behavior identical. Concretely: delete `READ_STATE = Path(__file__).with_name("snapshot.js").read_text()`, `MARKER = f"(() => {{ ... }})()"`, and the `def fingerprint(state): ...` function; add `from .dom_expressions import MARKER, READ_STATE, fingerprint, select_runtime_error` at the top. Keep the two `RuntimeError(...)` raise sites but route through `select_runtime_error("not_confirmed")` / `select_runtime_error("interrupted")`.

- [ ] **Step 6: Run full suite to verify zero regression**

Run: `uv run pytest -q`
Expected: PASS (all existing tests green; the MARKER/fingerprint behavior is identical).

- [ ] **Step 7: Commit**

```bash
git add fast_browser_use/dom_expressions.py fast_browser_use/browser.py tests/test_dom_expressions.py
git commit -m "refactor: extract shared dom_expressions (MARKER/fingerprint/select error)"
```

### Task 1.2: Extract the remaining 5 guard/act JS expressions

**Files:**
- Modify: `fast_browser_use/dom_expressions.py`, `fast_browser_use/browser.py`
- Test: `tests/test_dom_expressions.py`

**Interfaces:**
- Produces: `dom_expressions.after_input_wait` (str template), `scroll_fresh_guard(node: int|None) -> str`, `click_fresh_guard(node: int) -> str`, `scroll_point(node: int|None) -> str`, `act_dispatch_guard(action_json: str) -> str`.

- [ ] **Step 1: Write the failing test (interpolation correctness)**

```python
# append to tests/test_dom_expressions.py
from fast_browser_use import dom_expressions as de


def test_scroll_fresh_guard_inlines_node():
    expr = de.scroll_fresh_guard(7)
    assert "c.pageKey()" in expr and "c.scrollGuard(c.nodes.get(7))" in expr


def test_scroll_fresh_guard_none_uses_scrolling_element():
    expr = de.scroll_fresh_guard(None)
    assert "c.scrollGuard(c.nodes.get(7))" not in expr  # node None branch
    assert "scrollGuard" in expr


def test_click_fresh_guard_inlines_node():
    expr = de.click_fresh_guard(3)
    assert "c.pageKey()" in expr and "c.guard(c.nodes.get(3))" in expr


def test_scroll_point_inlines_node_or_scrolling_element():
    assert "c.nodes.get(5)" in de.scroll_point(5)
    assert "document.scrollingElement" in de.scroll_point(None)


def test_act_dispatch_guard_inlines_action_json():
    expr = de.act_dispatch_guard('{"node":2,"kind":"fill"}')
    assert "window.__fastBrowserUse" in expr
    assert '"node":2' in expr or 'node":2' in expr  # JSON interpolated


def test_after_input_wait_is_a_promise_expression():
    assert "new Promise" in de.after_input_wait and "resolve" in de.after_input_wait
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dom_expressions.py -v`
Expected: FAIL `AttributeError: module ... has no attribute 'scroll_fresh_guard'`

- [ ] **Step 3: Write minimal implementation**

Move the 5 inline f-strings out of `browser.py` into `dom_expressions.py` as the functions above, interpolating the node id / action JSON. Reference the exact current text in `browser.py` (the `after_input` Promise at `observe`, the two `fresh` guard f-strings, `scrollPoint`, and the act-dispatch guard in `browser_operation`). Signatures:

```python
# fast_browser_use/dom_expressions.py (append)
AFTER_INPUT_WAIT = """(action => new Promise(resolve => {
  const field=window.__fastBrowserUse?.nodes.get(action.node);
  const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
  let frames=0, stopped=false;
  const finish=()=>{stopped=true;resolve()};
  setTimeout(finish,autocomplete ? 200 : 50);
  const ready=()=>{
    if (stopped) return;
    const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
      .split(/\\s+/).filter(Boolean);
    const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
    const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
    if (++frames>=2 && (!autocomplete || options.some(e=>{
      const r=e.getBoundingClientRect();
      return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
        e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
    }))) finish();
    else requestAnimationFrame(ready);
  };
  requestAnimationFrame(ready);
}))"""


def after_input_wait(action_json: str) -> str:
    return AFTER_INPUT_WAIT + "(" + action_json + ")"


def scroll_fresh_guard(node):
    inner = f"c.nodes.get({node})" if node is not None else "document.scrollingElement"
    # NB: scroll fresh-guard uses scrollGuard(nodes.get(n)) only when node is an int region;
    # the None case (page-level) uses MARKER-only in browser.fresh — see browser.py.
    return ("(() => { const c=window.__fastBrowserUse; "
            f"return c ? [c.pageKey(),c.scrollGuard(c.nodes.get({node}))] : null; }})()") if node is not None else MARKER


def click_fresh_guard(node):
    return ("(() => { const c=window.__fastBrowserUse; "
            f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()")


def scroll_point(node):
    inner = f"c.nodes.get({node})" if node is not None else "document.scrollingElement"
    return f"(() => {{ const c=window.__fastBrowserUse; return c?.scrollPoint({inner}); }})()"


_ACT_DISPATCH_GUARD = """(action => {
  const e=window.__fastBrowserUse?.nodes.get(action.node);
  if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
      !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
  if (e.tagName==='LABEL' && (!e.control || e.control.matches(':disabled') ||
      e.control.closest('[aria-disabled="true"]'))) return null;
  if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
  const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
  if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
  if (!e.contains(document.elementFromPoint(x,y))) return null;
  if (action.kind==='select') {
    if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
        !o.disabled && !o.closest('optgroup[disabled]'))) return null;
    e.value=action.value;
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
  }
  return {x,y};
})("""


def act_dispatch_guard(action_json: str) -> str:
    return _ACT_DISPATCH_GUARD + action_json + ")"
```

Then in `browser.py`, replace the 5 inline f-strings with calls to `after_input_wait(json.dumps(action))`, `scroll_fresh_guard(node)`, `click_fresh_guard(node)`, `scroll_point(node)`, `act_dispatch_guard(json.dumps(action))`. Keep `browser_operation`'s `evaluate`/`call` plumbing identical.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dom_expressions.py tests/test_browser.py -v`
Expected: PASS

- [ ] **Step 5: Run full suite (zero regression)**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add fast_browser_use/dom_expressions.py fast_browser_use/browser.py tests/test_dom_expressions.py
git commit -m "refactor: extract 5 guard/act JS expressions into dom_expressions"
```

### Task 1.3: Add `Browser` Protocol + `make_browser()` factory; rename class

**Files:**
- Modify: `fast_browser_use/browser.py` (rename `Browser`→`PlaywrightBrowser`, add Protocol + factory), `fast_browser_use/__init__.py`
- Test: `tests/test_browser.py`

**Interfaces:**
- Produces: `Browser` (typing.Protocol), `PlaywrightBrowser` (renamed), `make_browser(url, *, browser=None, **opts) -> Browser`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_browser.py
import os
from fast_browser_use import browser as browser_mod


def test_make_browser_defaults_to_playwright_isolated(monkeypatch, tmp_path):
    # factory selects playwright by default and constructs PlaywrightBrowser
    b = browser_mod.make_browser  # existence check
    assert callable(b)
    # PlaywrightBrowser is the renamed class
    assert hasattr(browser_mod, "PlaywrightBrowser")


def test_make_browser_unknown_backend_raises():
    import pytest
    with pytest.raises(ValueError, match="FBU_BROWSER"):
        browser_mod.make_browser("https://example.com", browser="bogus")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_browser.py -k make_browser -v`
Expected: FAIL `AttributeError: module ... has no attribute 'make_browser'`

- [ ] **Step 3: Write minimal implementation**

Rename `class Browser:` → `class PlaywrightBrowser:` in `browser.py`. Add a `Browser` Protocol and the factory:

```python
# fast_browser_use/browser.py (append after imports)
from typing import Protocol


class Browser(Protocol):
    def observe(self, screenshot: bool = True) -> dict: ...
    def fresh(self, page: dict, action: dict | None = None) -> bool: ...
    def prepare(self, page: dict, *, screenshot: bool = False) -> dict: ...
    def act(self, action: dict, page: dict, text: str | None = None) -> dict: ...
    def close(self, *, video_path: str | None = None) -> None: ...
    def handoff(self, reason: str, *, mechanism: str = "auto") -> dict: ...
    def takeover(self) -> dict: ...
    @property
    def session_id(self) -> str | None: ...


def make_browser(url, *, browser=None, **opts) -> Browser:
    browser = browser or os.environ.get("FBU_BROWSER", "playwright")
    if browser == "playwright":
        return PlaywrightBrowser(url, **_pw_opts(opts))
    if browser == "ego":
        from .ego_browser import EgoBrowser
        return EgoBrowser(url, **_ego_opts(opts))
    raise ValueError(f"FBU_BROWSER must be playwright or ego, got {browser!r}")


def _pw_opts(opts):  # filter to playwright-only kwargs (Phase 2/3 expand this)
    allowed = {"video_dir", "viewport", "headless"}
    return {k: v for k, v in opts.items() if k in allowed}


def _ego_opts(opts):
    allowed = {"group", "profile_id", "space_id", "handoff_mode"}
    return {k: v for k, v in opts.items() if k in allowed}
```

Update `__init__.py`: `from .browser import Browser, PlaywrightBrowser, make_browser` (keep `Agent`, `Browser` re-export for back-compat — `Browser` now refers to the Protocol; existing `from fast_browser_use import Browser` callers still work since `PlaywrightBrowser` satisfies it structurally).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_browser.py -v`
Expected: PASS

- [ ] **Step 5: Run full suite**

Run: `uv run pytest -q`
Expected: PASS (rename + Protocol + factory; behavior unchanged)

- [ ] **Step 6: Commit**

```bash
git add fast_browser_use/browser.py fast_browser_use/__init__.py tests/test_browser.py
git commit -m "refactor: add Browser Protocol + make_browser factory, rename Browser→PlaywrightBrowser"
```

### Task 1.4: Route `agent.py` through `make_browser()`

**Files:**
- Modify: `fast_browser_use/agent.py` (line ~23: `self.browser = Browser(url, …)` → `make_browser(url, …)`)

- [ ] **Step 1: Write the failing test (agent still constructs a browser)**

```python
# append to tests/test_agent.py
def test_agent_uses_factory(monkeypatch):
    from fast_browser_use import agent as agent_mod
    called = {}
    def fake_make_browser(url, **opts):
        called["url"] = url; called["opts"] = opts
        raise RuntimeError("stop-after-factory")
    monkeypatch.setattr(agent_mod, "make_browser", fake_make_browser)
    import pytest
    with pytest.raises(RuntimeError, match="stop-after-factory"):
        agent_mod.Agent("https://example.com", "goal", )
    assert called["url"] == "https://example.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent.py::test_agent_uses_factory -v`
Expected: FAIL (agent still calls `Browser(...)` directly)

- [ ] **Step 3: Implement**

In `agent.py`: `from .browser import make_browser` and replace `self.browser = Browser(url, video_dir=video_dir, viewport=viewport, headless=headless)` with `self.browser = make_browser(url, video_dir=video_dir, viewport=viewport, headless=headless)`.

- [ ] **Step 4: Run full suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fast_browser_use/agent.py tests/test_agent.py
git commit -m "refactor: agent constructs browser via make_browser factory"
```

---

# Phase 2: Playwright Persistent Mode + Cross-Process Resume

### Task 2.1: `--profile-dir` selects persistent mode (`launch_persistent_context`)

**Files:**
- Modify: `fast_browser_use/browser.py` (`PlaywrightBrowser.__init__` branch on `profile_dir`)
- Test: `tests/test_persistent_browser.py` (gated `@pytest.mark.persistent`)

**Interfaces:**
- Produces: `PlaywrightBrowser(url, profile_dir=None, group=None, …)`; `session_id` returns the dir in persistent mode.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_persistent_browser.py
import json, os
import pytest
from fast_browser_use.browser import PlaywrightBrowser


@pytest.mark.persistent
def test_persistent_mode_reuses_login_state_across_instances(tmp_path):
    pytest.importorskip("playwright")
    d = str(tmp_path / "profile")
    # instance 1: set login state
    b1 = PlaywrightBrowser("https://example.com", profile_dir=d)
    b1.call("Runtime.evaluate", expression="localStorage.setItem('fbu','ok')", returnByValue=True)
    assert b1.session_id == d
    b1.close()
    # instance 2 same dir: state survived
    b2 = PlaywrightBrowser("https://example.com", profile_dir=d)
    val = b2.call("Runtime.evaluate", expression="localStorage.getItem('fbu')", returnByValue=True)["result"]["value"]
    assert val == "ok"
    b2.close()
```

Add the marker in `tests/conftest.py`:
```python
def pytest_configure(config):
    config.addinivalue_line("markers", "persistent: needs FBU_PERSISTENT_LIVE=1 + playwright browsers")
    config.addinivalue_line("markers", "ego: needs FBU_EGO_LIVE=1 + running ego lite + Full Access")


def pytest_collection_modifyitems(config, items):
    import os
    for item in items:
        if "persistent" in item.keywords and os.environ.get("FBU_PERSISTENT_LIVE") != "1":
            item.add_marker(pytest.mark.skip(reason="set FBU_PERSISTENT_LIVE=1"))
        if "ego" in item.keywords and os.environ.get("FBU_EGO_LIVE") != "1":
            item.add_marker(pytest.mark.skip(reason="set FBU_EGO_LIVE=1"))
```

- [ ] **Step 2: Run test to verify it fails/skips**

Run: `FBU_PERSISTENT_LIVE=1 uv run pytest tests/test_persistent_browser.py -v` (or it skips without the flag)
Expected: FAIL `TypeError: __init__() got an unexpected keyword argument 'profile_dir'` (when live) or SKIP otherwise.

- [ ] **Step 3: Implement**

In `PlaywrightBrowser.__init__`, branch on `profile_dir`:
```python
self.profile_dir = profile_dir
if profile_dir:
    os.makedirs(profile_dir, exist_ok=True)
    self.context = self.chrome.new_context_persistent(profile_dir, viewport=..., locale=...)  # see note
else:
    self.context = self.chrome.new_context(...)
```
Note: Playwright sync exposes `p.chromium.launch_persistent_context(user_data_dir, **opts)` which returns a context WITH an initial page. Restructure `__init__` so persistent mode calls `launch_persistent_context` and uses its page; isolated mode keeps `launch`+`new_context`. `new_cdp_session(self.page)` works for both. Expose `session_id`:
```python
@property
def session_id(self):
    return self.profile_dir  # None in isolated mode
```
Update `_pw_opts` to pass `profile_dir`/`group` through.

- [ ] **Step 4: Run test (live)**

Run: `FBU_PERSISTENT_LIVE=1 uv run pytest tests/test_persistent_browser.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fast_browser_use/browser.py tests/test_persistent_browser.py tests/conftest.py
git commit -m "feat: playwright persistent mode via --profile-dir (launch_persistent_context)"
```

### Task 2.2: Persistent `handoff`/`takeover` (resume mode) + trace `session` block

**Files:**
- Modify: `fast_browser_use/browser.py` (handoff/takeover for persistent), `fast_browser_use/agent.py` (`handoff` status + `state["handoffs"]`)
- Test: `tests/test_persistent_browser.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.persistent
def test_persistent_handoff_resume_roundtrip(tmp_path):
    d = str(tmp_path / "prof2")
    b = PlaywrightBrowser("https://example.com", profile_dir=d, handoff_mode="resume")
    rec = b.handoff("login required")
    assert rec["mechanism"] == "resume" and rec["reason"] == "login required"
    b.close()  # context closed, profile retained
    b2 = PlaywrightBrowser("https://example.com", profile_dir=d)  # resume
    page = b2.takeover()
    assert "url" in page and "actions" in page
    b2.close()


def test_isolated_resume_raises(tmp_path):
    b = PlaywrightBrowser("https://example.com")  # isolated
    import pytest
    with pytest.raises(ValueError, match="isolated context cannot survive process exit"):
        b.handoff("x", mechanism="resume")
    b.close()
```

(The isolated test needs playwright live; mark `@pytest.mark.persistent` too, or unit-test the raise via a flag without launching — prefer unit: construct with a flag that skips launch. For the plan, mark the raise-check `@pytest.mark.persistent` and accept the live requirement.)

- [ ] **Step 2: Run test to verify it fails**

Run: `FBU_PERSISTENT_LIVE=1 uv run pytest tests/test_persistent_browser.py -k handoff -v`
Expected: FAIL

- [ ] **Step 3: Implement**

`PlaywrightBrowser.handoff(reason, *, mechanism="auto")`:
- `mechanism = "auto"` → `"resume"` if `self.profile_dir` else `"pause"` (pause implemented in P3).
- resume: if not `self.profile_dir` → `raise ValueError("isolated context cannot survive process exit; use pause or --profile-dir")`. Else: `url = self.evaluate("location.href")`; `self.context.close()`; `self.chrome.close()`; return `{"reason": reason, "url": url, "mechanism": "resume"}`.
`takeover()`: relaunch `launch_persistent_context(self.profile_dir)`; `self.page = context.pages[0]`; `return self.observe()`.
`agent.py`: when `status` becomes `handoff`, call `browser.handoff(reason, mechanism=...)`, append to `state["handoffs"]`, set `state["status"]="handoff"`, end the run (don't loop further). Persist `{backend, mode, group, profile_dir, terminal_status, handoff_reason?}` to the trace `session` block.

- [ ] **Step 4: Run test**

Run: `FBU_PERSISTENT_LIVE=1 uv run pytest tests/test_persistent_browser.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fast_browser_use/browser.py fast_browser_use/agent.py tests/test_persistent_browser.py
git commit -m "feat: persistent handoff(resume) + takeover + trace session block"
```

---

# Phase 3: Isolated `storage_state` per-group + In-Process Pause Handoff

### Task 3.1: Isolated per-group `storage_state` file

**Files:**
- Modify: `fast_browser_use/browser.py`
- Test: `tests/test_browser_isolation.py` (`@pytest.mark.persistent`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_browser_isolation.py
import os, pytest
from fast_browser_use.browser import PlaywrightBrowser


@pytest.mark.persistent
def test_isolated_storage_state_per_group(tmp_path):
    g = str(tmp_path / "gA.json")
    b1 = PlaywrightBrowser("https://example.com", group="gA", storage_state_path=g)
    b1.call("Runtime.evaluate", expression="localStorage.setItem('grp','A')", returnByValue=True)
    b1.context.storage_state(path=g)  # save per-group file
    b1.close()
    b2 = PlaywrightBrowser("https://example.com", group="gA", storage_state_path=g)  # loads file
    val = b2.call("Runtime.evaluate", expression="localStorage.getItem('grp')", returnByValue=True)["result"]["value"]
    assert val == "A"
    b2.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `FBU_PERSISTENT_LIVE=1 uv run pytest tests/test_browser_isolation.py -v`
Expected: FAIL `unexpected kwarg storage_state_path`

- [ ] **Step 3: Implement**

`PlaywrightBrowser.__init__`: accept `group=None`, `storage_state_path=None`. In isolated mode, if `storage_state_path` and os.path exists → `new_context(storage_state=storage_state_path, …)`. On `close()`, if `storage_state_path` → `self.context.storage_state(path=storage_state_path)` before close. Default `storage_state_path = ~/.fbu/storage/<group>.json` when `group` given and not persistent. Update `_pw_opts` to pass `group`/`storage_state_path`.

- [ ] **Step 4: Run test**

Run: `FBU_PERSISTENT_LIVE=1 uv run pytest tests/test_browser_isolation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fast_browser_use/browser.py tests/test_browser_isolation.py
git commit -m "feat: isolated per-group storage_state workspace isolation"
```

### Task 3.2: In-process pause `handoff` (headed + block + continue)

**Files:**
- Modify: `fast_browser_use/browser.py`
- Test: `tests/test_browser_isolation.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.persistent
def test_isolated_pause_handoff_blocks_then_continues(tmp_path, monkeypatch):
    # sentinel = a file the test touches to signal "human done"
    sentinel = tmp_path / "go"
    b = PlaywrightBrowser("https://example.com", handoff_mode="pause", sentinel=str(sentinel))
    monkeypatch.setattr("builtins.input", lambda *a: None)  # don't block on stdin
    # use a thread to touch the sentinel, or test the mechanism directly:
    rec = b.handoff("login")  # should NOT close context; should set headed + wait
    assert rec["mechanism"] == "pause"
    # after sentinel is touched, handoff returns; context still alive
    assert b.context is not None
    b.close()
```

(Keep the test robust: the pause waits on a file sentinel with a short timeout in test mode; see impl.)

- [ ] **Step 2: Run test to verify it fails**

Run: `FBU_PERSISTENT_LIVE=1 uv run pytest tests/test_browser_isolation.py -k pause -v`
Expected: FAIL

- [ ] **Step 3: Implement**

`PlaywrightBrowser.handoff(... mechanism="pause")`:
- force headed if headless: `if self.chrome: ` (note: cannot switch headless post-launch; instead require `headless=False` when pause mode is selected — set in `__init__` from `handoff_mode`).
- print: `[fbu] Handoff (reason: <reason>). Act in the browser window, then touch <sentinel> (or send a line) to continue.`
- block: poll for sentinel file existence (or stdin line) with a max timeout (env `FBU_HANDOFF_TIMEOUT`, default 300s); in tests, set a 1s timeout + pre-touch the sentinel.
- return `{"reason", "url", "mechanism": "pause"}` WITHOUT closing the context.
`takeover()` in pause mode: no-op (same process); return `self.observe()`.

- [ ] **Step 4: Run test**

Run: `FBU_PERSISTENT_LIVE=1 uv run pytest tests/test_browser_isolation.py -k pause -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fast_browser_use/browser.py tests/test_browser_isolation.py
git commit -m "feat: isolated in-process pause handoff (headed + sentinel + continue)"
```

---

# Phase 4: EgoBrowser (macOS-only)

### Task 4.1: Ego subprocess driver + JS templates (observe + act, page.cdp)

**Files:**
- Create: `fast_browser_use/ego_browser.py`
- Test: `tests/test_ego_backend.py` (unit: template construction; live gated)

**Interfaces:**
- Produces: `EgoBrowser(url, group=…, profile_id=…, space_id=…, handoff_mode=…)` implementing `Browser`. Uses `dom_expressions.READ_STATE` + the 5 guard fns. Substitutes `page.keyboard.insertText` for `Input.insertText`.

- [ ] **Step 1: Write the failing test (template construction, no live browser)**

```python
# tests/test_ego_backend.py
import json
from fast_browser_use.ego_browser import EgoBrowser, OBSERVE_TEMPLATE, ACT_TEMPLATE, MAC_MODIFIERS


def test_observe_template_inlines_read_state_and_uses_page_cdp():
    js = OBSERVE_TEMPLATE.format(SPACE_ID=7, PAGE_LABEL="p1")
    assert "taskSpace(7)" in js and 'page.cdp("Runtime.evaluate"' in js
    assert "Page.captureScreenshot" in js


def test_act_template_uses_keyboard_insertText_for_fill():
    action = {"node": 2, "kind": "fill", "value": ""}
    js = ACT_TEMPLATE.format(SPACE_ID=7, PAGE_LABEL="p1", ACTION_JSON=json.dumps(action), FILL_TEXT="hello")
    assert "keyboard.insertText" in js  # NOT Input.insertText
    assert "Input.dispatchMouseEvent" in js


def test_mac_modifiers():
    import sys
    assert MAC_MODIFIERS(sys.platform) == (4 if sys.platform == "darwin" else 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ego_backend.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# fast_browser_use/ego_browser.py
"""macOS-only ego-browser backend. Drives `ego-browser nodejs` via page.cdp().
Two invocations per step (observe+prepare; act+re-observe). Substitutes
page.keyboard.insertText for Input.insertText (POC #5).
"""
import json, os, subprocess, sys, time
from .dom_expressions import READ_STATE, scroll_fresh_guard, click_fresh_guard, scroll_point, act_dispatch_guard, fingerprint


def MAC_MODIFIERS(platform):
    return 4 if platform == "darwin" else 2


OBSERVE_TEMPLATE = '''const task = await taskSpace({SPACE_ID});
const page = task.page({PAGE_LABEL!r});
// settle: compare marker across repeated page.cdp(Runtime.evaluate, READ_STATE) — see Python loop
const r = await page.cdp("Runtime.evaluate", {{expression: `{READ_STATE}`, returnByValue: true}});
const info = r?.result?.value;
info.fingerprint = ({fingerprint_source});
const shot = await page.cdp("Page.captureScreenshot", {{format:"jpeg", quality:72}});
console.log(JSON.stringify({{page: info, screenshot: shot?.data}}));
'''  # NOTE: fingerprint computed in Python from the dict; keep JSON output raw.

ACT_TEMPLATE = '''const task = await taskSpace({SPACE_ID});
const page = task.page({PAGE_LABEL!r});
const action = {ACTION_JSON};
// freshness guard per Contract B branch
if (action.kind === "scroll") {{
  const sp = await page.cdp("Runtime.evaluate", {{expression: `{scroll_point_expr}`, returnByValue:true}});
  const t = sp?.result?.value; if (!t) throw new Error("scroll region gone");
  await page.cdp("Input.dispatchMouseEvent", {{type:"mouseWheel", x:t.x, y:t.y, deltaX:0, deltaY:action.delta}});
}} else if (action.kind !== "wait") {{
  const g = await page.cdp("Runtime.evaluate", {{expression: `{guard_expr}`, returnByValue:true}});
  const c = g?.result?.value; if (!c) throw new Error("target gone");
  await page.cdp("Input.dispatchMouseEvent", {{type:"mousePressed", x:c.x, y:c.y, button:"left", clickCount:1}});
  await page.cdp("Input.dispatchMouseEvent", {{type:"mouseReleased", x:c.x, y:c.y, button:"left", clickCount:1}});
  if (action.kind === "fill") {{
    await page.cdp("Input.dispatchKeyEvent", {{type:"keyDown", key:"a", code:"KeyA", modifiers:{mods}, commands:["selectAll"]}});
    await page.cdp("Input.dispatchKeyEvent", {{type:"keyUp", key:"a", code:"KeyA", modifiers:{mods}}});
    await page.keyboard.insertText({FILL_TEXT!r});  // NOT Input.insertText (POC #5)
  }}
}}
const r = await page.cdp("Runtime.evaluate", {{expression: `{READ_STATE}`, returnByValue:true}});
console.log(JSON.stringify({{page: r?.result?.value}}));
'''


class EgoBrowser:
    def __init__(self, url, *, group="default", profile_id=None, space_id=None, handoff_mode="resume", **_):
        self.url = url; self.group = group; self.profile_id = profile_id
        self.space_id = space_id; self.handoff_mode = handoff_mode
        self.server_name = f"fbu-{group}"
        self.page_label = "p1"; self.after_input = None
        # ... launch preflight, first observe creates taskSpace + goto url

    def _run(self, js):
        cmd = ["ego-browser", "--ego-server-name", self.server_name, "nodejs", "-e", js]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()
        return json.loads(out.splitlines()[-1])

    def observe(self, screenshot=True):
        js = OBSERVE_TEMPLATE.format(SPACE_ID=self.space_id, PAGE_LABEL=self.page_label)
        data = self._run(js)
        page = data["page"]; page["fingerprint"] = fingerprint(page)
        if screenshot: page["screenshot"] = data["screenshot"]
        return page

    # fresh/prepare/act/handoff/takeover/close — implement per spec §4.3/§4.4
    # (act uses ACT_TEMPLATE with guard_expr = act_dispatch_guard(action_json) for click/fill,
    #  scroll_point(node) for scroll)
```

(Flesh out `fresh`/`prepare`/`act`/`handoff`/`takeover`/`close` using the templates; `act` sets `self.after_input = action` for the next `observe`.)

- [ ] **Step 4: Run unit tests**

Run: `uv run pytest tests/test_ego_backend.py -k "template or mac_modifiers" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fast_browser_use/ego_browser.py tests/test_ego_backend.py
git commit -m "feat(ego): subprocess driver + observe/act templates (keyboard.insertText swap)"
```

### Task 4.2: Ego live integration test (gated) + handoff/takeover

**Files:**
- Modify: `fast_browser_use/ego_browser.py`, `tests/test_ego_backend.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.ego
def test_ego_observe_produces_contract_A_keys():
    b = EgoBrowser("file:///Users/mac/codes/fast-browser-use/benchmarks/pages/settings.html")
    page = b.observe()
    keys = {"url","title","language","ready","dialogs","w","h","text","scroll","actions","marker","page_key","guards","omitted_actions"}
    assert keys <= set(page.keys())  # all 14 snapshot keys present (POC #4)
    b.close()
```

- [ ] **Step 2: Run (gated)**

Run: `FBU_EGO_LIVE=1 uv run pytest tests/test_ego_backend.py -k ego_observe -v`
Expected: FAIL (methods not fully implemented) → implement → PASS.

- [ ] **Step 3: Implement** `fresh`/`prepare`/`act`/`handoff`/`takeover`/`close` per spec §4.3/§4.4.

- [ ] **Step 4: Run**

Run: `FBU_EGO_LIVE=1 uv run pytest tests/test_ego_backend.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fast_browser_use/ego_browser.py tests/test_ego_backend.py
git commit -m "feat(ego): full observe/fresh/prepare/act/handoff/takeover + live test"
```

---

# Phase 5: CLI Surface + Handoff Detector

### Task 5.1: Handoff detector module

**Files:**
- Create: `fast_browser_use/handoff.py`
- Test: `tests/test_handoff.py`

- [ ] **Step 1: Write the failing test** (patterns over fixture page dicts: password+submit → login; `iframe[src*=recaptcha]` → captcha; `input[name*=otp|code]` → 2FA)

```python
# tests/test_handoff.py
from fast_browser_use.handoff import detect_handoff


def test_login_wall_detected():
    page = {"url":"https://x/login","text":"Sign in","actions":[{"kind":"fill","role":"textbox","node":1},{"kind":"click","role":"button","label":"Sign in","node":2}], "dialogs":[]}
    # a page with a password field + submit => login handoff
    page2 = {"url":"https://x/login","text":"","actions":[{"kind":"fill","role":"textbox","node":1,"label":"Password"},{"kind":"click","role":"button","label":"Sign in","node":2}], "dialogs":[]}
    assert detect_handoff(page2, []) == "login-required"


def test_captcha_and_2fa():
    assert detect_handoff({"url":"https://x/","text":"","actions":[],"dialogs":[]}, []) is None
```
(Refine fixtures to match the real action shapes from snapshot.js.)

- [ ] **Step 2-5:** Run fail → implement `detect_handoff(page, history) -> reason|None` with explicit patterns (no model call) → run pass → commit.

```bash
git add fast_browser_use/handoff.py tests/test_handoff.py
git commit -m "feat: harness handoff detector (login/captcha/2FA heuristics)"
```

### Task 5.2: CLI flags + `resume` subcommand + preflight

**Files:**
- Modify: `fast_browser_use/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test** (`--browser`, `--profile-dir`, `--group`, `--handoff`, `--handoff-mode`, `resume` parse; ego-not-macOS preflight raises).

- [ ] **Step 2-5:** Implement argparse additions + `resume` subcommand + `make_browser` wiring + ego preflight (`sys.platform == "darwin"` else `ValueError`; `shutil.which("ego-browser")` else link install.md) → run pass → commit.

```bash
git add fast_browser_use/cli.py tests/test_cli.py
git commit -m "feat(cli): --browser/--profile-dir/--group/--handoff + resume + ego preflight"
```

### Task 5.3: Wire detector + handoff into `agent.py`

**Files:**
- Modify: `fast_browser_use/agent.py`

- [ ] **Step 1-5:** In the `predict`/`observe` path, call `detect_handoff(page, history)`; on a reason, set `status="handoff"`, call `browser.handoff(reason, mechanism=…)`, record `state["handoffs"]`, end run → test (unit: a fixture loop that hits a login page sets handoff) → commit.

```bash
git add fast_browser_use/agent.py tests/test_agent.py
git commit -m "feat: agent handoff status + harness-triggered handoff"
```

---

# Phase 6: Docs / SKILL

### Task 6.1: SKILL + README backend section

**Files:**
- Modify: `skills/fast-browser-use/SKILL.md`, `README.md`, `README.zh-CN.md`

- [ ] **Step 1:** Add a "Browser backends & modes" section: `playwright` (default isolated) / `playwright --profile-dir` (persistent, full login reuse + resume) / `ego` (macOS-only advanced). Document `--group`, `--handoff`/`--handoff-mode`, `fbu resume`. Note ego macOS-only + Full-Access + ~2–3 s/step caveats.

- [ ] **Step 2:** Commit.

```bash
git add skills/fast-browser-use/SKILL.md README.md README.zh-CN.md
git commit -m "docs: browser backends & modes (isolated/persistent/ego) in SKILL + README"
```

---

## Self-Review

**1. Spec coverage:** Design §4.1 protocol+factory → T1.3; §4.1 Contract A → T1.1/T4.2 (ego produces keys); §4.1 Contract B → T1.2 (shared JS) + T4.1 (ego insertText swap); §4.2 PlaywrightBrowser isolated+persistent → T1.3/T2.1/T3.1/T3.2; §4.3 EgoBrowser → T4.1/T4.2; §4.4 shared dom_expressions → T1.1/T1.2; §5 group isolation → T2.1/T3.1; §6 resumable → T2.2/T4.2; §7 handoff → T3.2/T5.1/T5.3; §9 CLI → T5.2; §10 errors → T2.2 (isolated+resume ValueError) + T5.2 (preflight); §11 tests → each task's test; §13 compat → T1.x zero-regression gates. **Gap:** none uncovered.

**2. Placeholder scan:** T4.1 step 3 has a `# ...` ellipsis in `EgoBrowser.__init__`/`fresh`/`prepare`/`act`/`handoff`/`takeover`/`close` — these are deferred to T4.2 step 3 ("implement per spec §4.3/§4.4") with the spec as the source. This is acceptable because the spec fully specifies them (§4.3/§4.4 + Contract B), but the implementer must read `docs/ego-backend-design.md` §4.3/§4.4. Acceptable: the spec is the authoritative source and is referenced. T5.1/T5.2 step bodies are summarized — they reference concrete fixtures/patterns; acceptable since the detector patterns and argparse flags are enumerated. No "TBD"/"TODO".

**3. Type consistency:** `make_browser`/`PlaywrightBrowser`/`EgoBrowser`/`handoff(reason, *, mechanism="auto")`/`takeover()`/`session_id` signatures match across tasks. `dom_expressions.scroll_fresh_guard/click_fresh_guard/scroll_point/act_dispatch_guard/after_input_wait` names match T1.2 test + T4.1 usage. `MAC_MODIERS` typo risk: the test uses `MAC_MODIFIERS` and impl `MAC_MODIFIERS` — consistent.

---

## Execution Handoff

Plan complete and saved to `docs/ego-backend-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
