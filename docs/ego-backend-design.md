# Design: Three-Tier Browser Backend (playwright / playwright-persistent / ego)

Status: Revised (POC-locked) · Date: 2026-09-25 · Supersedes the ego-only design of `bf15a86`/`4e943b7` · Evidence: `docs/ego-backend-poc-findings.md` · Related: `docs/design.md`, `fast_browser_use/browser.py`, `fast_browser_use/agent.py`

## 1. Goal

Ship a **three-tier `FBU_BROWSER` backend** so the **same fbu** supports login-state reuse + human–AI collaboration + group isolation on **Windows + macOS (+ Linux)**:

1. **`playwright`** (default, unchanged) — isolated empty profile; benchmarks / CI / non-auth tasks.
2. **`playwright-persistent`** (NEW, primary for authenticated tasks) — `launch_persistent_context(user_data_dir)`; cross-platform login reuse + headed pause/resume collaboration.
3. **`ego`** (macOS-only advanced opt-in) — ego lite real sessions + native `handOff`/`takeOverTaskSpace`; ~2–3 s/step.

`fbu`'s `snapshot.js`, guards, `actions.py`, `verification.py`, and agent loop are **shared** across all three tiers.

## 2. Background & POC Evidence (locked)

Seven empirical experiments (see `docs/ego-backend-poc-findings.md`) drove this design:

| # | Finding | Effect |
| --- | --- | --- |
| 1 | ego lite is **macOS-only** (Windows=waitlist, no Linux) | ego **cannot** be the Windows backend → `playwright-persistent` is the cross-platform login-reuse path |
| 2 | ego `--ego-server-name` needs **Full Access** (fails in sandboxed agent hosts) | ego group isolation is macOS + Full-Access only; sandbox fallback = separate default-service task spaces |
| 3 | ego nodejs **cannot host a listening socket** (`net.createServer` → no output) | persistent IPC blocked → ego per-spawn (~0.5–1.2 s) unavoidable → ~2–3 s/step inherent |
| 4 | ego `page.cdp("Runtime.evaluate", READ_STATE)` produces **all 14** fbu page-dict keys | Contract A holds on ego; `snapshot.js` reuse is viable |
| 5 | ego `Input.insertText` CDP **times out**; `page.keyboard.insertText()` works | Contract B shared except one method swap on ego |
| 6 | ego `page.evaluate` preserves `window.*` across calls | `window.__fastBrowserUse` cache pattern works on ego |
| 7 | Playwright `launch_persistent_context` persists page-set cookie + localStorage across relaunch | **cross-platform login reuse viable without ego** → `playwright-persistent` tier |

## 3. Two Orthogonal Axes

- `FBU_BROWSER` — `playwright` (default) | `playwright-persistent` | `ego` (browser/driver axis, new)
- `FBU_BACKEND` — `auto`/`mlx`/`torch` (inference axis, unchanged)

Independent: any inference backend may run with any browser tier. `--browser` mirrors `FBU_BROWSER` on the CLI.

## 4. Architecture

```
agent.py  (unchanged; calls the Browser protocol)
    │
    ▼
Browser protocol  (observe / fresh / prepare / act / close  +  handoff / takeover)
    ├── PlaywrightBrowser          (current browser.py, renamed; default; cross-platform)
    ├── PlaywrightPersistentBrowser (NEW; launch_persistent_context; primary for auth; cross-platform)
    └── EgoBrowser                  (macOS-only opt-in; 2× ego-browser nodejs per step)
            │  injects fbu's snapshot.js via page.cdp("Runtime.evaluate")
            ▼
        ego lite desktop app (macOS, running, Full Access for --ego-server-name)
```

All three tiers consume the shared DOM/JS module (§4.5) and produce the identical page-dict contract (Contract A).

### 4.1 Browser protocol, factory, and contracts

```python
class Browser(Protocol):
    def observe(self, screenshot: bool = True) -> dict: ...        # page dict (Contract A)
    # fresh has THREE branches, each its own JS expression (see §4.5): action=None → MARKER only;
    # action.kind=='scroll' → pageKey+scrollGuard; action.kind in {click,select,fill} → pageKey+guard.
    def fresh(self, page: dict, action: dict | None = None) -> bool: ...
    def prepare(self, page: dict, *, screenshot: bool = False) -> dict: ...
    def act(self, action: dict, page: dict, text: str | None = None) -> dict: ...   # action: Contract B
    def close(self, *, video_path: str | None = None) -> None: ...
    # Collaboration hooks. Persistent uses headed pause/resume; ego uses native handOff/takeover;
    # isolated Playwright raises NotImplementedError (one-shot, no persisted session).
    def handoff(self, reason: str) -> dict: ...    # returns {reason, url}
    def takeover(self) -> dict: ...                # uses persisted session id; returns fresh page dict
    @property
    def session_id(self) -> str | None: ...         # persistent: profile_dir; ego: spaceId; isolated: None
```

**Construction & selection contract.** Tiers have different constructors, so `agent.py` never calls a backend constructor directly. A module-level factory selects and constructs:

```python
def make_browser(url: str, *, browser: str | None = None, **opts) -> Browser:
    # browser or FBU_BROWSER env (default "playwright"); opts filtered per tier:
    #   playwright:           {video_dir, viewport, headless}; ignores group/profile_dir/space_id/handoff_mode
    #   playwright-persistent:{profile_dir, group, video_dir?, viewport?, headless=False for handoff, handoff_mode}
    #   ego:                  {group, profile_id?, space_id?, handoff_mode}; ignores viewport/headless/video_dir
```

**Contract A — page dict.** Produced identically by all tiers (same `snapshot.js` via `Runtime.evaluate`; screenshot/fingerprint/preparation added by the driver). Every key must match:

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

| kind | distinguishing fields | execution path (identical across tiers, except the ego note) |
| --- | --- | --- |
| `click` / `fill` | `node:int`, `role`, `value`, `rect` | resolve center `{x,y}` via the act-dispatch guard JS; `Input.dispatchMouseEvent` mousePressed+Released; if `fill`: selectAll (`KeyA` + `commands:["selectAll"]`, mac modifiers=4 else 2) + insert text (isolated/persistent: `Input.insertText`; **ego: `page.keyboard.insertText(text)`** — raw `Input.insertText` CDP times out per POC #5) |
| `select` | `node:int`, `value`, `role='combobox'` | **page-side JS, NOT CDP Input**: validate option exists & enabled; `e.value=action.value`; `dispatchEvent(input)` + `dispatchEvent(change)`; return `{x,y}` (coords unused). Failure → `RuntimeError("Dropdown execution was not confirmed")` |
| `scroll` | `node:int\|None`, `delta:int`, `scroll_state` | resolve scroll point via `scrollPoint` JS (`document.scrollingElement` when node=None); `Input.dispatchMouseEvent(type="mouseWheel", **point, deltaX=0, deltaY=delta)` |
| `wait` | (no `node`) | Python `time.sleep(0.1)` (no CDP) |

### 4.2 PlaywrightBrowser (default, cross-platform)

`fast_browser_use/browser.py` renamed to `PlaywrightBrowser`, implements the protocol. **No behavioral change.** `call`/`evaluate`/`browser_operation` stay private. `handoff`/`takeover` raise `NotImplementedError` (one-shot; no persisted session). `session_id` = `None`.

### 4.3 PlaywrightPersistentBrowser (NEW — cross-platform login reuse + collaboration)

This is the **recommended tier for authenticated tasks on Windows + macOS + Linux**. It uses `chromium.launch_persistent_context(user_data_dir=…)` instead of `launch()` + `new_context()`:

- **Login reuse**: `user_data_dir` per group (`FBU_PROFILE_DIR`, default `~/.fbu/profiles/<group>`). Pointing it at a **copy** of the user's real Chrome/Edge profile yields an already-logged-in session (POC #7). **Never** the live profile — Chromium locks it.
- **CDP**: a persistent context still exposes `new_cdp_session(page)`, so `observe`/`fresh`/`prepare`/`act` run the **same** `Runtime.evaluate`/`Page.captureScreenshot`/`Input.*`/`Emulation.*` calls as `PlaywrightBrowser` — Contract A/B are byte-identical (no `insertText` swap; that is ego-only).
- **Headed for handoff**: `FBU_HANDOFF=auto` forces `headless=False` so the human can see and interact. The window persists across `fbu resume` because the profile dir persists.
- **`handoff(reason)`**: set `status=handoff`, print the resume instruction, close the context (the profile persists on disk). No `taskSpace`/`finish` — the "session" is the profile dir.
- **`takeover()`**: relaunch `launch_persistent_context(same user_data_dir)`; the page state (cookies/localStorage/scroll) survived; re-`observe`. `session_id` = the profile dir path.
- **`close(video_path)`**: saves Playwright video as today.

### 4.4 EgoBrowser (macOS-only advanced opt-in)

Holds: `group` (default `default`), `space_id` (None until created/resumed), `page_label` (default `p1`), `profile_id`? (optional), `server_name` (`fbu-<group>`). The ego lite app must be running; **`--ego-server-name` requires Full Access** (fails in sandboxed hosts — POC #2; sandbox fallback uses the default service with separate task spaces).

Stdin streaming is blocked and `net.createServer` is blocked (POC #3), so each agent step uses **two `ego-browser nodejs` invocations** (observe+prepare; then act+re-observe):

- **Invocation A — observe+prepare** (returns page dict): `taskSpace(space_id)` → `page(page_label)` → settle loop comparing `marker` across repeated `page.cdp("Runtime.evaluate", READ_STATE)` → `info = page.cdp("Runtime.evaluate", {expression: READ_STATE, returnByValue:true})` → `page.cdp("Page.captureScreenshot", {format:"jpeg",quality:72})`.
- **Python: model inference** (`choose` ~70 ms; `field_text` when typing).
- **Invocation B — act+re-observe** (returns new page dict): freshness guard per Contract B branch via `page.cdp("Runtime.evaluate", GUARD_EXPR)` → dispatch via `page.cdp("Input.*")` (and `page.keyboard.insertText(text)` for `fill`, **not** `Input.insertText`) → re-observe.

`window.__fastBrowserUse` persists across A and B (same persistent page in ego lite; resets on navigation, re-init by `||=`, detected by `page_key`). `after_input` is a Python field on `EgoBrowser` (set in B, consumed in next A) — it never crosses a process boundary as JS state.

- **`handoff(reason)`**: `await task.handOff()`, read `page.url()`, return `{reason, url}`; keep the space.
- **`takeover()`**: `takeOverTaskSpace(self.space_id)` → `task.userPage()` → resolve label via `await task.tabs()` (match `targetId`), `await task.adopt(page,{as})` if unmanaged and **update `self.page_label`** (the human may have switched/opened a tab ≠ `p1`); then `observe()` and return the fresh page dict. `page_label` is persisted to the trace `ego` block.
- **`close`**: on `done`, `task.finish({keep:[]})`; on `blocked`/`handoff`, keep the space for `fbu resume`. `terminal_status` + `handoff_reason` persisted to the trace.

### 4.5 Shared DOM / JS expression module

`snapshot.js` is already shared (read at module load into `READ_STATE`). `browser.py` embeds **six additional inline JS expression strings** as Python f-strings used by `fresh`/`act`/`observe`. All three tiers must run the **same** expressions; extracting them avoids drift:

| Expression | Current location | Used by | Purpose |
| --- | --- | --- | --- |
| `MARKER` | `browser.py` module const | `fresh` (no-action branch) | cheap page-identity probe (`state?.marker ?? null`) |
| `after_input` Promise | `browser.observe` (inline) | `observe` (post-fill) | bounded wait for combobox autocomplete |
| scroll fresh-guard | `browser.fresh` (inline f-string) | `fresh` (scroll) | `[pageKey(), scrollGuard(nodes.get(n))]` |
| click/select/fill fresh-guard | `browser.fresh` (inline) | `fresh` (element) | `[pageKey(), guard(nodes.get(n))]` |
| `scrollPoint` (act) | `browser_operation` (inline) | `act` (scroll) | resolve a hittable wheel point |
| act-dispatch guard | `browser_operation` (inline) | `act` (click/fill/select) | resolve center coords; **select** sets value + dispatches input/change in-page |

These move to `fast_browser_use/dom_expressions.py` (Python constants/templates); all tiers interpolate node ids / action JSON. **Only the Python driver differs** (isolated/persistent: `session.send`; ego: subprocess + `page.cdp`), and on ego the final `fill` text insert uses `page.keyboard.insertText`. `fingerprint()` and the `StalePage`/select-`RuntimeError` mapping (§10) are also shared.

## 5. Group Isolation

Unified cross-platform semantics — **one isolated profile per group**:

- **`playwright` / `playwright-persistent`**: `FBU_PROFILE_DIR` (default `~/.fbu/profiles/<group>`). One directory per group → separate cookie jars, caches, and (persistent) login state. **Cross-platform, no sandbox issue.** Different groups never share state.
- **`ego`**: `--ego-server-name=fbu-<group>` (separate ego lite named service = separate windows/process context) **requires Full Access**; optional `FBU_EGO_PROFILE=<id>` adds cookie-jar isolation (ego Skill cautions against programmatic profile selection, so it is opt-in). **Sandbox fallback**: separate task spaces in the default service (weaker — shared profile) when Full Access is unavailable.

Configuration: `FBU_BROWSER`, `FBU_EGO_GROUP`/`FBU_PROFILE_DIR` (per tier), `FBU_EGO_PROFILE` (ego, advanced), `FBU_HANDOFF`. `--browser`/`--group`/`--profile-dir`/`--handoff` mirror on `run`/`resume`.

## 6. Resumable Session Model

The persistent and ego tiers are **resumable** across CLI invocations (required for human–AI collaboration). The isolated `playwright` tier remains one-shot.

- `fbu run '<url>' --goal '…' --browser playwright-persistent --group personal`:
  - Launches `launch_persistent_context(~/.fbu/profiles/personal)`; persists the profile dir to the `--trace` file's `session` block (`{tier, group, profile_dir, terminal_status, handoff_reason?}`).
  - On `done` it closes the context (profile retained on disk for reuse). On `blocked`/`handoff` it exits **keeping the profile** for `fbu resume`.
- `fbu resume --browser playwright-persistent --group personal` (or `fbu run --resume [<trace>]` reading the trace's `session` block):
  - Re-launches `launch_persistent_context(same profile_dir)`; cookies/localStorage survived; continues from the human's page. (ego: `takeOverTaskSpace(space_id)` + `userPage()`, resolving/adopting the label; see §4.4.)
- `fbu run --browser ego --group g1`: persists `spaceId` to the trace's `ego` block `{space_id, group, profile_id, server_name, page_label, terminal_status, handed_off, handoff_reason?}`.

## 7. Human–AI Handoff

The model never selects a synthetic `HANDOFF` action — that would break zero-hallucination. Handoff is **harness-triggered**, gated by `FBU_HANDOFF=auto` (default; `never` disables; `always` hands off at the first observe):

1. **Heuristic detection** during `observe`/`prepare`: lightweight patterns over `page["text"]`/`url`/`actions`/`dialogs` indicating a login form (password field + submit), captcha, 2FA, or auth-redirect. Explicit, overridable, no model call.
2. **`BLOCKED` status** from the agent loop (3 repeated no-change actions) → handoff with reason `blocked-no-progress`.

On trigger: `agent.py` sets `state["status"]="handoff"`, records `{reason, url, elapsed_ms}` in `state["handoffs"]`; `browser.handoff(reason)` runs the tier-specific handoff (persistent: close context, profile retained; ego: `task.handOff()`); the CLI prints the resume instruction; the run ends with the trace carrying the session id + reason. `fbu resume` → `browser.takeover()` → continue.

`PlaywrightBrowser.handoff()` raises `NotImplementedError` (one-shot; point at `--browser playwright-persistent` or `ego`).

## 8. Data Flow

```
fbu run --browser playwright-persistent --group g1
  └─ PlaywrightPersistentBrowser.observe → page dict
       └─ agent.predict (model ~70ms) / agent.act
            └─ PlaywrightPersistentBrowser.act → new page dict
                 └─ if handoff: status=handoff → close context (profile retained) → exit
fbu resume --browser playwright-persistent --group g1
  └─ relaunch launch_persistent_context(same profile_dir) → takeover → fresh page dict
       └─ continue agent loop from current page

# ego path (macOS, Full Access for --server-name):
fbu run --browser ego --group g1
  └─ EgoBrowser.observe (invocation A) → page dict
       └─ agent.predict / agent.act
            └─ EgoBrowser.act (invocation B) → new page dict
                 └─ if handoff: task.handOff() → exit, keep space
fbu resume <spaceId> --browser ego --group g1
  └─ EgoBrowser.takeover → takeOverTaskSpace → userPage → fresh page dict
```

## 9. CLI Surface

`fast_browser_use/cli.py` gains:

- `--browser {playwright,playwright-persistent,ego}` (default `playwright`, mirrors `FBU_BROWSER`).
- `--group <name>` (all tiers; selects the profile dir / ego server-name).
- `--profile-dir <path>` (persistent/ego; overrides the default per-group dir).
- `--handoff {auto,never,always}` (default `auto`; persistent forces headed).
- New subcommand `resume`: `fbu resume [--browser B] [--group G] [<spaceId|trace>]` (ego takes a `spaceId`; persistent/ego both accept `--resume` on `run` reading the trace's `session`/`ego` block).
- `record` and `serve` keep isolated-`playwright`-only semantics; `--browser` other than `playwright` there raises a clear error.
- `fbu doctor`-style preflight: ego checks (`ego lite` running? `ego-browser` on PATH? Full Access for `--server-name`? macOS only?) and persistent checks (profile dir writable? not a live Chrome profile?).

## 10. Error Handling

| Failure | Behavior |
| --- | --- |
| ego lite not running / `ego-browser` not on PATH / not macOS | `EgoBrowser` preflight fails fast with the install.md remediation link |
| ego `--ego-server-name` in a sandboxed host | clear message: needs Full Access or non-sandboxed shell; offer sandbox fallback (separate default-service task spaces) |
| `taskSpace`/`takeOverTaskSpace` rejected (user didn't approve) | surface the ego message; status `blocked`; do not retry/route around (per ego Skill) |
| ego subprocess non-JSON / non-zero exit | `StalePage` if recoverable (re-observe once); else `blocked` with raw stderr in the trace |
| `Runtime.evaluate` `exceptionDetails` | `StalePage("Document changed during evaluation")` — identical to Playwright (all tiers) |
| `act` select: option gone (`Runtime.evaluate` null) | `RuntimeError("Dropdown execution was not confirmed; inspect before retry.")` (shared) |
| `act` select: evaluate interrupted (`exceptionDetails`) | `RuntimeError("Dropdown execution was interrupted; inspect before retry.")` (shared) |
| ego `act` fill: `Input.insertText` timeout | fall back to `page.keyboard.insertText(text)` (POC #5; the ego driver does this by default) |
| persistent: `user_data_dir` is a live Chrome profile | `ValueError` at preflight (Chromium locks it; use a copy or a dedicated dir) |
| ego `close(video_path=…)` non-None | `ValueError("ego backend cannot record Playwright video")` |
| ego receives `viewport`/`headless` kwargs | silently ignored (ego lite owns its window); `make_browser` drops them |
| handoff requested on isolated `playwright` | `NotImplementedError` → point at `--browser playwright-persistent` or `ego` |

`StalePage` semantics are reused unchanged across all tiers.

## 11. Testing Strategy

- `test_browser.py` — `PlaywrightBrowser` rename keeps behavior (no regressions).
- `test_persistent_browser.py` (new):
  - Unit: profile-dir resolution per group; handoff detector over fixture `page` dicts; trace `session` block round-trip.
  - Live (`@pytest.mark.persistent`, skip unless `FBU_PERSISTENT_LIVE=1`): cookie+localStorage survive relaunch on the target platform (re-verifies POC #7); headed handoff→resume reconnects the same profile.
- `test_ego_backend.py` (new):
  - Unit: JS template generation (snapshot.js injection, guard per action kind, settle-loop boundaries); group→server-name/profile mapping; trace `ego` block round-trip; `Input.insertText`→`keyboard.insertText` substitution.
  - Live (`@pytest.mark.ego`, skip unless `FBU_EGO_LIVE=1` and app running + Full Access): real `ego-browser nodejs` round-trip — observe→act→re-observe on `benchmarks/pages/settings.html` (re-verifies POC #4/#5), plus handoff→resume.
- `test_cli.py` — `--browser`, `--group`, `--profile-dir`, `resume` parsing + preflight error paths (ego-not-macOS, live-profile rejection).
- `test_agent.py` — `handoff` status handling; isolated path stays one-shot.
- Shared-JS tests: `test_dom_expressions.py` — assert all three tiers interpolate the same expressions (no drift).

## 12. Non-Goals

- ego as the Windows backend — no Windows build (waitlist). Deferred until ego lite ships Windows.
- Sub-second per-step latency on ego — accepted (persistent IPC blocked, POC #3).
- Reverse-engineering ego lite's named-service socket — no black-box IPC.
- Model-driven handoff — rejected (breaks zero-hallucination).
- Programmatic ego profile creation — `FBU_EGO_PROFILE` references existing ids only.
- Pointing `user_data_dir` at a live Chrome profile — rejected (lock + safety).
- OpenWiki page edits — regenerated by the scheduled workflow.

## 13. Compatibility & Migration

- Default `FBU_BROWSER=playwright`: zero change to existing runs, benchmarks, `fbu record`, SKILL, CI.
- The `Browser` rename + `dom_expressions.py` extraction is the only refactor to existing code; `agent.py` is unchanged at call sites.
- `playwright-persistent` is purely additive (new class + factory branch).
- `ego` is macOS-only and opt-in; the preflight rejects it elsewhere.
- SKILL/OpenWiki get a short "Browser tiers" section: default isolated; `playwright-persistent` for auth (cross-platform); `ego` macOS-only advanced.
- No dependency additions for the default/persistent paths; the ego path needs only the external `ego-browser` CLI. `pyproject.toml` unchanged.
