# Design: Two-Backend Browser (playwright isolated/persistent + ego) — Cross-Platform Login Reuse + Human–AI Collaboration

Status: Revised (POC-locked, 2-backend) · Date: 2026-09-25 · Supersedes the three-tier design of `66b5c36` · Evidence: `docs/ego-backend-poc-findings.md` · Related: `docs/design.md`, `fast_browser_use/browser.py`, `fast_browser_use/agent.py`

## 1. Goal

Ship **two browser backends** under one `FBU_BROWSER` axis so the **same fbu** supports login-state reuse + human–AI collaboration + group isolation on **Windows + macOS (+ Linux)**:

1. **`playwright`** (default, cross-platform Win/macOS/Linux) — one backend, two profile modes:
   - **`isolated`** (default): `launch()` + `new_context()`; optional per-group `storage_state` file (lightweight isolation); headed in-process-pause collaboration.
   - **`persistent`** (via `--profile-dir`): `launch_persistent_context(user_data_dir)`; per-group profile dir (full login reuse); in-process pause **+ cross-process resume**.
2. **`ego`** (macOS-only advanced opt-in) — ego lite real sessions + native `handOff`/`takeOverTaskSpace`; ~2–3 s/step; `--ego-server-name` group isolation needs Full Access.

fbu's `snapshot.js`, guards, `actions.py`, `verification.py`, and agent loop are **shared** across both backends and both playwright modes.

## 2. Background & POC Evidence (locked)

Nine empirical experiments (see `docs/ego-backend-poc-findings.md`) drove this design:

| # | Finding | Effect |
| --- | --- | --- |
| 1 | ego lite is **macOS-only** (Windows=waitlist, no Linux) | ego **cannot** be the Windows backend → `playwright` is the cross-platform answer |
| 2 | ego `--ego-server-name` needs **Full Access** (fails in sandboxed hosts) | ego group isolation is macOS + Full-Access only; sandbox fallback = separate default-service task spaces |
| 3 | ego nodejs **cannot host a listening socket** | persistent IPC blocked → ego per-spawn unavoidable → ~2–3 s/step inherent |
| 4 | ego `page.cdp("Runtime.evaluate", READ_STATE)` produces **all 14** fbu page-dict keys | Contract A holds on ego; `snapshot.js` reuse is viable |
| 5 | ego `Input.insertText` CDP **times out**; `page.keyboard.insertText()` works | Contract B shared except one method swap on ego |
| 6 | ego `page.evaluate` preserves `window.*` across calls | `window.__fastBrowserUse` cache pattern works on ego |
| 7 | Playwright `launch_persistent_context` persists page-set cookie + localStorage across relaunch | **persistent mode** = cross-platform full login reuse + cross-process resume |
| 8 | Isolated `new_context(storage_state=<per-group-file>)` gives per-group isolation **without** a persistent dir | **isolated mode** = lightweight per-group isolation on the default tier |
| 9 | Isolated `launch(headless=False)` + in-process block supports collaboration **without** a persistent profile | **isolated mode** = in-process-pause collaboration (headed + block + continue) |

## 3. Two Orthogonal Axes

- `FBU_BROWSER` — `playwright` (default) | `ego` (backend/driver axis)
- `FBU_PROFILE_MODE` (playwright only) — `isolated` (default) | `persistent` (selected by `--profile-dir`); auto-set by `make_browser` from the presence of a profile dir.
- `FBU_BACKEND` — `auto`/`mlx`/`torch` (inference axis, unchanged)

Independent: any inference backend may run with any browser backend/mode. `--browser` mirrors `FBU_BROWSER`; `--profile-dir` selects persistent mode on `playwright`.

## 4. Architecture

```
agent.py  (unchanged; calls the Browser protocol)
    │
    ▼
Browser protocol  (observe / fresh / prepare / act / close  +  handoff / takeover)
    ├── PlaywrightBrowser          (current browser.py, renamed; cross-platform)
    │     ├── mode=isolated   → launch()+new_context(storage_state?); per-group storage_state file; in-process pause
    │     └── mode=persistent → launch_persistent_context(user_data_dir); per-group profile dir; in-process pause + cross-process resume
    └── EgoBrowser                  (macOS-only opt-in; 2× ego-browser nodejs per step)
            │  injects fbu's snapshot.js via page.cdp("Runtime.evaluate")
            ▼
        ego lite desktop app (macOS, running, Full Access for --ego-server-name)
```

Both backends consume the shared DOM/JS module (§4.4) and produce the identical page-dict contract (Contract A).

### 4.1 Browser protocol, factory, and contracts

```python
class Browser(Protocol):
    def observe(self, screenshot: bool = True) -> dict: ...        # page dict (Contract A)
    # fresh has THREE branches, each its own JS expression (see §4.4): action=None → MARKER only;
    # action.kind=='scroll' → pageKey+scrollGuard; action.kind in {click,select,fill} → pageKey+guard.
    def fresh(self, page: dict, action: dict | None = None) -> bool: ...
    def prepare(self, page: dict, *, screenshot: bool = False) -> dict: ...
    def act(self, action: dict, page: dict, text: str | None = None) -> dict: ...   # action: Contract B
    def close(self, *, video_path: str | None = None) -> None: ...
    # Collaboration hooks. mechanism = "pause" (in-process, headed) or "resume" (cross-process).
    #   playwright isolated: pause only (ephemeral context cannot survive process exit).
    #   playwright persistent: pause + resume.
    #   ego: resume (native handOff/takeover); pause via task.handOff kept in-process is not supported.
    def handoff(self, reason: str, *, mechanism: str = "auto") -> dict: ...    # returns {reason, url, mechanism}
    def takeover(self) -> dict: ...                # persistent/ego only; relaunch/restore session, return fresh page dict
    @property
    def session_id(self) -> str | None: ...         # persistent: profile_dir; ego: spaceId; isolated: storage_state file path or None
```

**Construction & selection contract.** Backends/modes have different constructors, so `agent.py` never calls a constructor directly:

```python
def make_browser(url: str, *, browser: str | None = None, **opts) -> Browser:
    # browser or FBU_BROWSER env (default "playwright"); opts filtered per backend:
    #   playwright (isolated):   {group?, storage_state?(per-group file), viewport?, headless, handoff_mode, video_dir?}
    #   playwright (persistent): {group?, profile_dir(~/.fbu/profiles/<group>), viewport?, headless=False if pause, handoff_mode, video_dir?}
    #     — presence of profile_dir (or --profile-dir) selects persistent mode (FBU_PROFILE_MODE=persistent);
    #       absence selects isolated mode (FBU_PROFILE_MODE=isolated).
    #   ego: {group, profile_id?, space_id?, handoff_mode}; ignores profile_dir/storage_state/viewport/headless/video_dir
```

**Contract A — page dict.** Produced identically by both backends and both playwright modes (same `snapshot.js` via `Runtime.evaluate`; screenshot/fingerprint/preparation added by the driver). Every key must match:

| Key | Type | Producer | Consumer |
| --- | --- | --- | --- |
| `url` | str | snapshot.js | model/agent (prompt, trace) |
| `title` | str | snapshot.js | model/agent (prompt) |
| `language` | str | snapshot.js | model (prompt) |
| `ready` | bool | snapshot.js | browser.prepare |
| `dialogs` | list[{label,modal}] | snapshot.js | **handoff detector** (modal → handoff) |
| `w`,`h` | int | snapshot.js | trace / viewport diagnostics |
| `text` | str | snapshot.js | model (prompt), `fingerprint` |
| `scroll` | {y,height} | snapshot.js | `fingerprint`, browser |
| `actions` | list[dict] | snapshot.js | model/agent (action space), `fingerprint` |
| `marker` | list | snapshot.js | browser.fresh (MARKER branch) |
| `page_key` | list | snapshot.js | browser.fresh (all branches) |
| `guards` | dict | snapshot.js | browser.fresh/act |
| `omitted_actions` | int | snapshot.js | trace |
| `fingerprint` | str | driver (`fingerprint()`) | agent (stale detection, trace) |
| `screenshot` | str(b64) | driver (`Page.captureScreenshot`, jpeg q72) | agent/recording |
| `preparation` | dict | browser.prepare (latency_ms/samples/settled) | trace (observations) |

**Contract B — action dict.** Each `page["actions"]` entry is one of four variants; `act()` dispatches per variant. `rect` present on element actions but stripped from `marker`; `id` assigned by snapshot.js after splice (`e1..eN`, `scroll_*`, `wait`):

| kind | distinguishing fields | execution path (identical across backends/modes, except the ego note) |
| --- | --- | --- |
| `click` / `fill` | `node:int`, `role`, `value`, `rect` | resolve center `{x,y}` via the act-dispatch guard JS; `Input.dispatchMouseEvent` mousePressed+Released; if `fill`: selectAll (`KeyA` + `commands:["selectAll"]`, mac modifiers=4 else 2) + insert text (playwright isolated/persistent: `Input.insertText`; **ego: `page.keyboard.insertText(text)`** — raw `Input.insertText` CDP times out per POC #5) |
| `select` | `node:int`, `value`, `role='combobox'` | **page-side JS, NOT CDP Input**: validate option exists & enabled; `e.value=action.value`; `dispatchEvent(input)` + `dispatchEvent(change)`; return `{x,y}` (coords unused). Failure → `RuntimeError("Dropdown execution was not confirmed")` |
| `scroll` | `node:int\|None`, `delta:int`, `scroll_state` | resolve scroll point via `scrollPoint` JS (`document.scrollingElement` when node=None); `Input.dispatchMouseEvent(type="mouseWheel", **point, deltaX=0, deltaY=delta)` |
| `wait` | (no `node`) | Python `time.sleep(0.1)` (no CDP) |

### 4.2 PlaywrightBrowser (default, cross-platform; isolated + persistent modes)

`fast_browser_use/browser.py` renamed to `PlaywrightBrowser`, implements the protocol. **One class, two modes** selected by `profile_dir`:

- **isolated mode** (`profile_dir=None`, default): `launch()` + `new_context(storage_state=<per-group file> if group else None, viewport, locale, …)` as today. `call`/`evaluate`/`browser_operation` stay private. One behavioral change: `handoff`/`takeover` are **enabled in pause mode** (not `NotImplementedError`).
- **persistent mode** (`profile_dir=<dir>`, via `--profile-dir` or `FBU_PROFILE_DIR`): `launch_persistent_context(user_data_dir=<dir>, viewport, …)` instead of `launch()`+`new_context()`. A persistent context still exposes `new_cdp_session(page)`, so `observe`/`fresh`/`prepare`/`act` run the **same** `Runtime.evaluate`/`Page.captureScreenshot`/`Input.*`/`Emulation.*` calls — Contract A/B are byte-identical (no `insertText` swap; that is ego-only).

`fbu` never points `user_data_dir` at a **live** Chrome profile (Chromium locks it); the persistent mode uses a per-group dir, optionally seeded from a copy of the user's real profile.

**Per-group isolation (both modes):**
- isolated: `~/.fbu/storage/<group>.json` via `context.storage_state(path=…)` on close and `new_context(storage_state=…)` on launch — lightweight cookies+localStorage per group (POC #8).
- persistent: `~/.fbu/profiles/<group>/` via `user_data_dir` — full profile per group (POC #7).
- Without `--group`: isolated is fully ephemeral (today's behavior); persistent uses a single default profile dir.

**Collaboration (`handoff`/`takeover`):**
- `--handoff-mode pause` (default on isolated; available on persistent): set `status=handoff`, force `headless=False`, print the resume/handoff instruction, and **block in-process** (wait for a sentinel: stdin line, file touch, or timeout). The human acts in the visible window; on the sentinel, `fbu` continues the **same process** with the **same context/page** — no re-`observe` needed beyond a fresh read. `takeover()` is a no-op (same process). `handoff()` returns `{reason, url, mechanism:"pause"}`. (POC #9 verified headed + in-process block.)
- `--handoff-mode resume` (default on persistent; **ValueError on isolated**): `handoff(reason)` closes the context (profile retained on disk), returns `{reason, url, mechanism:"resume"}`; `fbu resume` relaunches `launch_persistent_context(same user_data_dir)` and continues. `takeover()` performs that relaunch + `observe`. (POC #7 verified state survives relaunch.)
- `session_id`: isolated = the per-group `storage_state` file path (or `None` when ephemeral); persistent = the `user_data_dir`.

### 4.3 EgoBrowser (macOS-only advanced opt-in)

Holds: `group` (default `default`), `space_id` (None until created/resumed), `page_label` (default `p1`), `profile_id`? (optional), `server_name` (`fbu-<group>`). The ego lite app must be running; **`--ego-server-name` requires Full Access** (fails in sandboxed hosts — POC #2; sandbox fallback uses the default service with separate task spaces).

Stdin streaming is blocked and `net.createServer` is blocked (POC #3), so each agent step uses **two `ego-browser nodejs` invocations** (observe+prepare; then act+re-observe):

- **Invocation A — observe+prepare** (returns page dict): `taskSpace(space_id)` → `page(page_label)` → settle loop comparing `marker` across repeated `page.cdp("Runtime.evaluate", READ_STATE)` → `info = page.cdp("Runtime.evaluate", {expression: READ_STATE, returnByValue:true})` → `page.cdp("Page.captureScreenshot", {format:"jpeg",quality:72})`.
- **Python: model inference** (`choose` ~70 ms; `field_text` when typing).
- **Invocation B — act+re-observe** (returns new page dict): freshness guard per Contract B branch via `page.cdp("Runtime.evaluate", GUARD_EXPR)` → dispatch via `page.cdp("Input.*")` (and `page.keyboard.insertText(text)` for `fill`, **not** `Input.insertText`) → re-observe.

`window.__fastBrowserUse` persists across A and B (same persistent page in ego lite; resets on navigation, re-init by `||=`, detected by `page_key`). `after_input` is a Python field on `EgoBrowser` (set in B, consumed in next A) — never crosses a process boundary as JS state.

- **`handoff(reason, mechanism="resume")`**: `await task.handOff()`, read `page.url()`, return `{reason, url, mechanism:"resume"}`; keep the space. (ego collaboration is resume-only via native handOff/takeover; pause-in-process is not supported.)
- **`takeover()`**: `takeOverTaskSpace(self.space_id)` → `task.userPage()` → resolve label via `await task.tabs()` (match `targetId`), `await task.adopt(page,{as})` if unmanaged and **update `self.page_label`** (the human may have switched/opened a tab ≠ `p1`); then `observe()` and return the fresh page dict. `page_label` is persisted to the trace `ego` block.
- **`close`**: on `done`, `task.finish({keep:[]})`; on `blocked`/`handoff`, keep the space for `fbu resume`. `terminal_status` + `handoff_reason` persisted to the trace.

### 4.4 Shared DOM / JS expression module

`snapshot.js` is already shared (read at module load into `READ_STATE`). `browser.py` embeds **six additional inline JS expression strings** as Python f-strings used by `fresh`/`act`/`observe`. Both backends must run the **same** expressions; extracting them avoids drift:

| Expression | Current location | Used by | Purpose |
| --- | --- | --- | --- |
| `MARKER` | `browser.py` module const | `fresh` (no-action branch) | cheap page-identity probe (`state?.marker ?? null`) |
| `after_input` Promise | `browser.observe` (inline) | `observe` (post-fill) | bounded wait for combobox autocomplete |
| scroll fresh-guard | `browser.fresh` (inline f-string) | `fresh` (scroll) | `[pageKey(), scrollGuard(nodes.get(n))]` |
| click/select/fill fresh-guard | `browser.fresh` (inline) | `fresh` (element) | `[pageKey(), guard(nodes.get(n))]` |
| `scrollPoint` (act) | `browser_operation` (inline) | `act` (scroll) | resolve a hittable wheel point |
| act-dispatch guard | `browser_operation` (inline) | `act` (click/fill/select) | resolve center coords; **select** sets value + dispatches input/change in-page |

These move to `fast_browser_use/dom_expressions.py` (Python constants/templates); both backends interpolate node ids / action JSON. **Only the Python driver differs** (playwright: `session.send`; ego: subprocess + `page.cdp`), and on ego the final `fill` text insert uses `page.keyboard.insertText`. `fingerprint()` and the `StalePage`/select-`RuntimeError` mapping (§10) are also shared.

## 5. Group Isolation

Unified cross-platform semantics on `playwright`; ego is macOS/Full-Access:

- **`playwright` isolated**: `~/.fbu/storage/<group>.json` per group (cookies + localStorage). Cross-platform, no sandbox issue. Different groups never share sessions.
- **`playwright` persistent**: `~/.fbu/profiles/<group>/` per group (full profile). Cross-platform.
- **`ego`**: `--ego-server-name=fbu-<group>` (separate ego lite named service = separate windows/process context) **requires Full Access**; optional `FBU_EGO_PROFILE=<id>` adds cookie-jar isolation (ego Skill cautions against programmatic profile selection, so it is opt-in). **Sandbox fallback**: separate task spaces in the default service (weaker — shared profile) when Full Access is unavailable.

Configuration: `FBU_BROWSER`, `FBU_PROFILE_DIR`/`--profile-dir` (persistent mode), `FBU_EGO_GROUP`/`--group` (group name), `FBU_EGO_PROFILE` (ego, advanced), `FBU_HANDOFF`/`FBU_HANDOFF_MODE`. `--group`/`--profile-dir`/`--handoff`/`--handoff-mode` mirror on `run`/`resume`.

## 6. Resumable Session Model

Persistent (resume mode) and ego are **resumable** across CLI invocations. Isolated and persistent (pause mode) collaborate **in-process** (no resume needed — same process never exited).

- `fbu run --browser playwright --profile-dir ~/.fbu/profiles/g1 --group g1` (persistent, resume mode): launches the persistent context; persists `{backend, mode, group, profile_dir, terminal_status, handoff_reason?}` to the `--trace` file's `session` block. On `done` closes the context (profile retained). On `blocked`/`handoff` exits **keeping the profile** for `fbu resume`.
- `fbu resume --browser playwright --group g1` (or `fbu run --resume [<trace>]` reading the trace's `session` block): relaunches `launch_persistent_context(same profile_dir)`; cookies/localStorage survived; continues from the human's page.
- `fbu run --browser playwright --group g1` (isolated, pause mode): on handoff, opens the headed window and **blocks in-process**; the human acts; on the sentinel, the same process continues. No `fbu resume` needed.
- `fbu run --browser ego --group g1`: persists `spaceId` to the trace `ego` block `{space_id, group, profile_id, server_name, page_label, terminal_status, handed_off, handoff_reason?}`; `fbu resume <spaceId>` → `takeOverTaskSpace`.

## 7. Human–AI Handoff

The model never selects a synthetic `HANDOFF` action — that would break zero-hallucination. Handoff is **harness-triggered**, gated by `FBU_HANDOFF=auto` (default; `never` disables; `always` hands off at the first observe):

1. **Heuristic detection** during `observe`/`prepare`: lightweight patterns over `page["text"]`/`url`/`actions`/`dialogs` indicating a login form (password field + submit), captcha, 2FA, or auth-redirect. Explicit, overridable, no model call.
2. **`BLOCKED` status** from the agent loop (3 repeated no-change actions) → handoff with reason `blocked-no-progress`.

The **mechanism** is selected by `FBU_HANDOFF_MODE` (default `auto`: `pause` for isolated, `resume` for persistent, `resume` for ego):

- **`pause`** (playwright isolated/persistent): `agent.py` sets `state["status"]="handoff"`, records `{reason, url, mechanism, elapsed_ms}` in `state["handoffs"]`; `browser.handoff(reason, mechanism="pause")` forces headed, prints "act in the browser window, then signal continue (touch <sentinel> / send a line / wait)", and **blocks** until the sentinel. On resume the same context/page continues. No process exit.
- **`resume`** (playwright persistent / ego): `handoff(reason, mechanism="resume")` closes the context / `task.handOff()`, returns `{reason, url, mechanism}`; the run ends with the trace carrying the session id + reason; the CLI prints the resume instruction; `fbu resume` → `browser.takeover()` → continue.

`PlaywrightBrowser` isolated + `resume` raises `ValueError("isolated context cannot survive process exit; use pause or --profile-dir")`.

## 8. Data Flow

```
# playwright isolated, pause mode (cross-platform, in-process)
fbu run --browser playwright --group g1
  └─ PlaywrightBrowser(isolated, storage_state=~/.fbu/storage/g1.json).observe → page dict
       └─ agent.predict / agent.act → act → new page dict
            └─ if handoff: status=handoff → headed window → block in-process → human acts → sentinel → continue same process

# playwright persistent, resume mode (cross-platform, cross-process)
fbu run --browser playwright --profile-dir ~/.fbu/profiles/g1 --group g1
  └─ PlaywrightBrowser(persistent).observe → page dict
       └─ agent.predict / agent.act → act → new page dict
            └─ if handoff: close context (profile retained) → exit
fbu resume --browser playwright --group g1
  └─ relaunch launch_persistent_context(same profile_dir) → takeover → fresh page dict → continue

# ego (macOS, Full Access for --server-name)
fbu run --browser ego --group g1
  └─ EgoBrowser.observe (invocation A) → page dict
       └─ agent.predict / agent.act
            └─ EgoBrowser.act (invocation B) → new page dict
                 └─ if handoff: task.handOff() → exit, keep space
fbu resume <spaceId> --browser ego --group g1
  └─ EgoBrowser.takeover → takeOverTaskSpace → userPage → fresh page dict → continue
```

## 9. CLI Surface

`fast_browser_use/cli.py` gains:

- `--browser {playwright,ego}` (default `playwright`, mirrors `FBU_BROWSER`).
- `--profile-dir <path>` (playwright persistent mode; selects `FBU_PROFILE_MODE=persistent`; absent = isolated).
- `--group <name>` (all backends: storage_state file or profile dir per group / ego server-name).
- `--handoff {auto,never,always}` (trigger) and `--handoff-mode {auto,pause,resume}` (mechanism; default `auto`).
- New subcommand `resume`: `fbu resume [--browser B] [--group G] [--profile-dir D] [<spaceId|trace>]` (ego takes a `spaceId`; playwright reads the trace `session` block; `fbu run --resume [<trace>]` is the alias).
- `record` and `serve` keep isolated-`playwright`-only semantics; `--browser ego` or `--profile-dir` there raises a clear error.
- `fbu doctor`-style preflight: ego checks (`ego lite` running? `ego-browser` on PATH? Full Access for `--server-name`? macOS only?) and persistent checks (profile dir writable? not a live Chrome profile?).

## 10. Error Handling

| Failure | Behavior |
| --- | --- |
| ego lite not running / `ego-browser` not on PATH / not macOS | `EgoBrowser` preflight fails fast with the install.md remediation link |
| ego `--ego-server-name` in a sandboxed host | clear message: needs Full Access or non-sandboxed shell; offer sandbox fallback (separate default-service task spaces) |
| `taskSpace`/`takeOverTaskSpace` rejected (user didn't approve) | surface the ego message; status `blocked`; do not retry/route around |
| ego subprocess non-JSON / non-zero exit | `StalePage` if recoverable (re-observe once); else `blocked` with raw stderr in the trace |
| `Runtime.evaluate` `exceptionDetails` | `StalePage("Document changed during evaluation")` — identical across backends/modes |
| `act` select: option gone (`Runtime.evaluate` null) | `RuntimeError("Dropdown execution was not confirmed; inspect before retry.")` (shared) |
| `act` select: evaluate interrupted (`exceptionDetails`) | `RuntimeError("Dropdown execution was interrupted; inspect before retry.")` (shared) |
| ego `act` fill: `Input.insertText` timeout | fall back to `page.keyboard.insertText(text)` (POC #5; ego driver default) |
| persistent: `user_data_dir` is a live Chrome profile | `ValueError` at preflight (Chromium locks it; use a copy or a dedicated dir) |
| isolated + `--handoff-mode resume` | `ValueError("isolated context cannot survive process exit; use pause or --profile-dir")` |
| ego `close(video_path=…)` non-None | `ValueError("ego backend cannot record Playwright video")` |
| ego receives `viewport`/`headless` kwargs | silently ignored (ego lite owns its window); `make_browser` drops them |

`StalePage` semantics are reused unchanged across both backends/modes.

## 11. Testing Strategy

- `test_browser.py` — `PlaywrightBrowser` rename + isolated mode keeps behavior (no regressions).
- `test_persistent_browser.py` (new): persistent mode (`profile_dir`); per-group dir; in-process pause; cross-process resume (re-verifies POC #7). `@pytest.mark.persistent`, skip unless `FBU_PERSISTENT_LIVE=1`.
- `test_browser_isolation.py` (new): isolated mode per-group `storage_state` file round-trip (re-verifies POC #8); in-process-pause handoff (re-verifies POC #9).
- `test_ego_backend.py` (new): JS template generation; group→server-name/profile mapping; `Input.insertText`→`keyboard.insertText`; trace `ego` block round-trip. `@pytest.mark.ego`, skip unless `FBU_EGO_LIVE=1` + app running + Full Access.
- `test_cli.py` — `--browser`, `--profile-dir`, `--group`, `--handoff-mode`, `resume` parsing + preflight error paths.
- `test_agent.py` — `handoff` status (pause + resume); isolated-pause stays one process; persistent-resume exits cleanly.
- `test_dom_expressions.py` — assert both backends interpolate the same expressions (no drift).

## 12. Non-Goals

- ego as the Windows backend — no Windows build (waitlist). Deferred until ego lite ships Windows.
- Sub-second per-step latency on ego — accepted (persistent IPC blocked, POC #3).
- Three-tier split — superseded; `playwright-persistent` is now `playwright` + persistent mode, not a separate tier.
- Reverse-engineering ego lite's named-service socket — no black-box IPC.
- Model-driven handoff — rejected (breaks zero-hallucination).
- Programmatic ego profile creation — `FBU_EGO_PROFILE` references existing ids only.
- Pointing `user_data_dir` at a live Chrome profile — rejected (lock + safety).
- OpenWiki page edits — regenerated by the scheduled workflow.

## 13. Compatibility & Migration

- Default `FBU_BROWSER=playwright` (isolated, no `--profile-dir`, no `--group`): zero change to existing runs, benchmarks, `fbu record`, SKILL, CI.
- The `Browser` rename + `dom_expressions.py` extraction is the only refactor to existing code; `agent.py` is unchanged at call sites.
- `--profile-dir` (persistent mode) and `--group` (storage_state/profile dir per group) and handoff (pause/resume) are purely additive.
- `ego` is macOS-only and opt-in; the preflight rejects it elsewhere.
- SKILL/OpenWiki get a short "Browser backends & modes" section: `playwright` isolated (default) / `playwright --profile-dir` (persistent, full login reuse + resume) / `ego` (macOS-only advanced).
- No dependency additions for the playwright paths; the ego path needs only the external `ego-browser` CLI. `pyproject.toml` unchanged.
