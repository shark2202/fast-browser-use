# Design: ego-browser as an Optional Browser Backend (Group Isolation + Human–AI Collaboration)

Status: Proposed · Date: 2026-09-25 · Supersedes: none · Related: `docs/design.md`, `fast_browser_use/browser.py`, `fast_browser_use/agent.py`

## 1. Goal

Add **ego-browser** as an optional, opt-in browser backend for `fbu`, alongside the existing Playwright backend. The ego backend uniquely supports:

1. **Group isolation** — multiple independent browser instances/sessions keyed by a group name, so parallel teams or tasks do not share windows, tabs, or login state.
2. **Human–AI collaboration** — the loop can hand control to a human (for login, 2FA, captcha, paywalls), exit cleanly, and **resume** from where the human left off.

Playwright remains the default backend. Existing behavior, benchmarks, and deployments are unchanged when the ego backend is not selected.

## 2. Background & Justification

### 2.1 Why ego-browser is feasible (API surface)

Empirical testing against a running `ego lite` desktop app confirmed ego-browser's `page.cdp()` escape hatch can carry fbu's entire guarded-DOM logic:

| fbu CDP dependency | ego-browser path | Verified |
| --- | --- | --- |
| `Runtime.evaluate(READ_STATE)` (snapshot.js + guards) | `page.cdp("Runtime.evaluate", {expression, returnByValue})` | ✅ returns title, ~4 ms |
| `Page.captureScreenshot` | `page.cdp("Page.captureScreenshot", {format, quality})` | ✅ 81 KB jpeg |
| `Input.dispatchMouseEvent` / `dispatchKeyEvent` / `insertText` | `page.cdp("Input.*", …)` | ✅ `{}` ok |
| `Emulation.setFocusEmulationEnabled` | `page.cdp("Emulation.*", …)` | ✅ ok |

Critically, `page.evaluate()` **preserves `window.*` state across calls** (verified: a counter incremented across two separate `page.evaluate()` calls held its value). This means fbu's `window.__fastBrowserUse` WeakMap/Map cache pattern (the heart of `snapshot.js` node-identity + guard logic) works unchanged through ego-browser. The page dict contract (url/text/actions/scroll/marker/page_key/guards/fingerprint/screenshot/ready) is produced identically because `snapshot.js` runs in the page and returns the same structured value.

`fbu` therefore reuses `snapshot.js`, `actions.py`, `verification.py`, and all guard logic verbatim. Only the **driver** that issues CDP calls changes.

### 2.2 Why ego-browser is not a drop-in (execution model)

| | Playwright (current) | ego-browser |
| --- | --- | --- |
| Call path | sync Python `session.send()` → in-process Chromium WebSocket | shell → new Node.js process per `ego-browser nodejs` → IPC → ego lite app |
| Per-call overhead | sub-millisecond | ~0.5–1.2 s process startup |
| stdin persistent protocol | n/a (in-process) | **blocked** — embedded node runtime does not wire parent stdin to `process.stdin` (verified: piped input yields empty `data` event) |
| Platform | Linux headless / Windows / macOS | macOS desktop app required (Linux/Windows unsupported by install path) |
| Login state | isolated empty profile | **reuses user's real logged-in profiles** (the unique value) |

Because the agent loop interleaves browser calls with **local model inference in Python** (single-token forward pass, ~70 ms), the loop cannot be pre-batched into one script — each action depends on the model's just-computed choice. The ego backend therefore issues **two `ego-browser nodejs` invocations per agent step** (observe+prepare; then act+re-observe), projecting ~2–3 s/step vs Playwright's ~0.5 s. This is the documented, accepted cost of the ego backend, justified by its login-state-reuse and collaboration features.

## 3. Two Orthogonal Axes

`fbu` already uses `FBU_BACKEND` to mean the **inference** backend (`auto`/`mlx`/`torch`, choosing the model). The browser is a **separate, orthogonal axis**, introduced here:

- `FBU_BROWSER` — `playwright` (default) | `ego`
- `FBU_BACKEND` — unchanged (inference)

The two are independent: any inference backend may run with any browser backend. `--browser` mirrors the env on the CLI, exactly as `--backend` mirrors `FBU_BACKEND`.

## 4. Architecture

```
agent.py  (unchanged; calls the Browser protocol)
    │
    ▼
Browser  (abstract protocol: observe / fresh / prepare / act / close  +  handoff / takeover  [ego-only])
    ├── PlaywrightBrowser   (current browser.py, renamed; default)
    └── EgoBrowser           (new)
            │  holds: spaceId, group, profileId?, --ego-server-name, page label
            │  per step: 2 × ego-browser nodejs subprocess (JS templates)
            │  injects fbu's existing snapshot.js via page.cdp("Runtime.evaluate")
            ▼
        ego lite desktop app (must be running)
```

### 4.1 Browser protocol

The abstract surface is the **semantic** operations `agent.py` already calls — not the CDP plumbing (`call`/`evaluate`/`session`), which is Playwright-specific:

```python
class Browser(Protocol):
    def observe(self, screenshot: bool = True) -> dict: ...        # returns page dict (see contract A)
    # fresh has THREE branches, each its own JS expression (see §4.5): action=None → MARKER only;
    # action.kind=='scroll' → pageKey+scrollGuard; action.kind in {click,select,fill} → pageKey+guard.
    def fresh(self, page: dict, action: dict | None = None) -> bool: ...
    def prepare(self, page: dict, *, screenshot: bool = False) -> dict: ...
    def act(self, action: dict, page: dict, text: str | None = None) -> dict: ...   # action: see contract B
    def close(self, *, video_path: str | None = None) -> None: ...
    # ego-only collaboration hooks (Playwright raises NotImplementedError; only EgoBrowser implements).
    # reason is determined by the harness detector (§7), passed in; url is read from ego page.info().
    def handoff(self, reason: str) -> dict: ...    # runs task.handOff(); returns {reason, url}
    def takeover(self) -> dict: ...                # uses self.space_id; returns fresh page dict
    @property
    def session_id(self) -> str | None: ...        # ego: spaceId; playwright: None
```

**Construction & selection contract.** The two backends have **different constructors** (Playwright: `viewport`/`headless`/`video_dir`; ego: `group`/`profile_id`/`space_id`/`handoff_mode`), so `agent.py` never calls a backend constructor directly. A module-level factory selects and constructs:

```python
def make_browser(url: str, *, browser: str | None = None, **opts) -> Browser:
    # browser or FBU_BROWSER env (default "playwright"); opts passed through per backend.
    # Playwright: opts = {video_dir, viewport, headless}; ignores group/profile/space_id/handoff_mode.
    # Ego:       opts = {group, profile_id, space_id, handoff_mode}; ignores viewport/headless/video_dir
    #            (raises on video_dir — ego cannot record Playwright video; see §10).
```
`agent.py` calls `make_browser(url, browser=…, **resolved_opts)`; the resolved opts are filtered by the factory so each backend receives only its kwargs.

**Contract A — page dict.** Produced identically by both backends (same `snapshot.js` evaluated via `Runtime.evaluate`; screenshot/fingerprint/preparation added by the driver). `EgoBrowser` produces it through `page.cdp("Runtime.evaluate", …)`, exactly as Playwright does through `session.send`. Every key must match:

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

`EgoBrowser` MUST emit all 16 rows identically; missing `title`/`dialogs`/`language` would break the model prompt and the handoff detector.

**Contract B — action dict.** Each entry in `page["actions"]` is one of four heterogeneous variants; `act()` dispatches per variant. `rect` is present on element actions but stripped from `marker`. `id` is assigned by snapshot.js after splice (`e1..eN`, `scroll_*`, `wait`):

| kind | distinguishing fields | execution path (identical on both backends) |
| --- | --- | --- |
| `click` / `fill` | `node:int`, `role`, `value`, `rect` | resolve center `{x,y}` via the act-dispatch guard JS; `Input.dispatchMouseEvent` mousePressed+Released; if `fill`: then selectAll (`KeyA` + `commands:["selectAll"]`, mac modifiers=4 else 2) + `Input.insertText(text)` |
| `select` | `node:int`, `value`, `role='combobox'` | **page-side JS, NOT CDP Input**: validate option exists & enabled; `e.value=action.value`; `dispatchEvent(input)` + `dispatchEvent(change)`; return `{x,y}` (coords unused). Failure → `RuntimeError("Dropdown execution was not confirmed")` |
| `scroll` | `node:int\|None`, `delta:int`, `scroll_state` | resolve scroll point via `scrollPoint` JS (`document.scrollingElement` when node=None); `Input.dispatchMouseEvent(type="mouseWheel", **point, deltaX=0, deltaY=delta)` |
| `wait` | (no `node`) | Python `time.sleep(0.1)` (no CDP) |

`EgoBrowser.act` reimplements this dispatch via `page.cdp("Input.*")` and `page.cdp("Runtime.evaluate")` using the **shared JS expressions** in §4.5 — it does **not** reuse `browser_operation`'s Python (which is Playwright-`session`-specific).

### 4.2 PlaywrightBrowser

`fast_browser_use/browser.py` is renamed to `PlaywrightBrowser` and made to implement the protocol. No behavioral change. `call`/`evaluate`/`browser_operation` stay as private helpers. `handoff`/`takeover` raise `NotImplementedError` (Playwright has no persistent session to hand off; one-shot process semantics remain).

### 4.3 EgoBrowser

Holds per-session state and drives ego via subprocess. Key fields: `group` (str, default `default`), `space_id` (int, None until created/resumed), `page_label` (default `"p1"`), `profile_id` (optional), `server_name` (derived `fbu-<group>`).

**Lifecycle:**
- Construct: store config; do NOT spawn yet (lazy).
- First `observe`: spawn a "setup" invocation that creates/reuses `taskSpace(name, {profileId?})` under `--ego-server-name=fbu-<group>`, navigates `p1` to the URL, returns `spaceId` (persisted to trace).
- Each `observe`/`fresh`/`prepare`/`act`: spawn a bounded nodejs invocation running a JS template (see 4.4), parse JSON from stdout.
- `handoff`: spawn `task.handOff()` invocation; set internal `handed_off=True`; return `{reason, url}`.
- `takeover`: spawn `takeOverTaskSpace(self.space_id)` + read `task.userPage()`; resolve its label via `await task.tabs()` (match by `targetId`). If unmanaged, `await task.adopt(page, {as})` and **update `self.page_label`** to the resulting permanent label (the human may have opened a new tab ≠ `p1`); if `p1` still exists and is active, keep it. Then `observe()` against the resolved label and return the fresh page dict. `page_label` is persisted to the trace `ego` block so a later `resume` reuses it.
- `close`: on `done`, spawn `task.finish({keep: []})` (task complete → space closed); on `blocked` or `handoff`, **do not `finish()`** — keep the space so `fbu resume` can continue. The terminal status (`done`/`blocked`/`handoff`) and, when set, the handoff reason are persisted to the trace `ego` block.

### 4.4 Per-step invocation batching (latency mitigation)

Stdin streaming is blocked (§2.2), so each step uses two invocations:

**Invocation A — observe+prepare** (JS template, returns page dict):
```js
const task = await taskSpace(SPACE_ID);                 // resume
const page = task.page(PAGE_LABEL);                    // or userPage() after takeover
// settle loop (bounded): compare marker across repeated page.cdp("Runtime.evaluate", READ_STATE)
// then: info = page.cdp("Runtime.evaluate", {expression: READ_STATE, returnByValue: true})
//       shot = page.cdp("Page.captureScreenshot", {format:"jpeg", quality:72})
console.log(JSON.stringify({page: info.value, screenshot: shot.data, preparation:{...}}));
```

**Python: model inference** (`choose`, ~70 ms; `field_text` when typing).

**Invocation B — act+re-observe** (JS template, returns new page dict):
```js
const task = await taskSpace(SPACE_ID);
const page = task.page(PAGE_LABEL);
// freshness guard: page.cdp("Runtime.evaluate", {expression: GUARD_EXPR_FOR_ACTION})
// dispatch per action.kind per Contract B (§4.1) using shared JS (§4.5):
//   click/fill → act-dispatch guard (coords) + Input.mousePressed/Released (+ selectAll + insertText for fill)
//   select     → act-dispatch guard JS sets e.value + dispatchEvent (no CDP Input)
//   scroll     → scrollPoint JS + Input.mouseWheel
// re-observe: info = page.cdp("Runtime.evaluate", {expression: READ_STATE, returnByValue: true})
console.log(JSON.stringify({executed: action.id, page: info.value, ...}));
```

`window.__fastBrowserUse` (the snapshot.js cache) persists across A and B because both evaluate in the **same persistent page** owned by ego lite; it resets only on navigation, which snapshot.js re-initializes (`||=`) and `fresh` detects via `page_key`.

**`after_input` across invocations (autocomplete wait).** `browser.observe` consumes `self.after_input` (set by `act` for non-`wait` kinds) to debounce-wait for combobox autocomplete options before reading. In the ego 2-invocation model this is a **Python field on `EgoBrowser`**: invocation B (`act`) sets `self.after_input = action`; the next invocation A (`observe`) reads it and, when set, prepends the bounded autocomplete-wait JS (the `after_input` Promise expression, §4.5) before the normal observe. The field never crosses a process boundary as JS state — it is carried in Python between subprocess calls — so the existing semantics are preserved exactly.

### 4.5 Shared DOM / JS expression module

`snapshot.js` is already shared (read at module load into `READ_STATE`). But `browser.py` currently embeds **six additional inline JS expression strings** as Python f-strings, used by `fresh`/`act`/`observe`. `EgoBrowser` must run the **same** expressions via `page.cdp("Runtime.evaluate")`; duplicating them inline would risk guard divergence and broken freshness semantics. The refactor extracts them into a shared module both backends import:

| Expression | Current location | Used by | Purpose |
| --- | --- | --- | --- |
| `MARKER` | `browser.py` module const | `fresh` (no-action branch) | cheap page-identity probe (`state?.marker ?? null`) |
| `after_input` Promise | `browser.observe` (inline) | `observe` (post-fill) | bounded wait for combobox autocomplete options |
| scroll fresh-guard | `browser.fresh` (inline f-string) | `fresh` (scroll) | `[pageKey(), scrollGuard(nodes.get(n))]` |
| click/select/fill fresh-guard | `browser.fresh` (inline f-string) | `fresh` (element) | `[pageKey(), guard(nodes.get(n))]` |
| `scrollPoint` (act) | `browser_operation` (inline) | `act` (scroll) | resolve a hittable wheel point for a scroll region |
| act-dispatch guard | `browser_operation` (inline) | `act` (click/fill/select) | resolve center coords; **select** sets value + dispatches input/change in-page |

These move to `fast_browser_use/dom_expressions.py` (Python constants/templates) or a sibling `.js`; `PlaywrightBrowser` and `EgoBrowser` both interpolate node ids / action JSON into them. **Only the Python driver differs** (Playwright: `session.send`; ego: subprocess + `page.cdp`). The `fingerprint()` helper and the `StalePage`/select-`RuntimeError` mapping (§10) are also shared. This keeps both backends byte-identical on DOM semantics and is a prerequisite for `EgoBrowser.act` correctness (Contract B).

## 5. Group Isolation (Decision 1 = A, refined)

Isolation has two layers; both are explicitly requested by the user:

- **Primary axis — `--ego-server-name=fbu-<group>`** (always applied). Each group is a **separate named browser service** in ego lite = separate windows, separate process context. Fully CLI-controllable; no profile management needed. This is the default group boundary.
- **Secondary axis — `profileId`** (opt-in via `FBU_EGO_PROFILE=<id>` or `--profile`). Adds cookie-jar/cache isolation within a service. The ego-browser Skill cautions against inspecting/selecting profiles unless explicitly requested; fbu treats it as an advanced opt-in. A group may pin one profile; without it, ego lite's default profile applies.

Configuration:
- `FBU_EGO_GROUP=<name>` (default `default`) → `--ego-server-name=fbu-<name>`
- `FBU_EGO_PROFILE=<id>` (optional) → `taskSpace(name, {profileId: id})`
- `--group` / `--profile` mirror on the `run` and `resume` CLI subcommands.

Same group across runs → same `--ego-server-name` → same task space reused (via persisted `spaceId` in the trace). Different groups → fully isolated services.

## 6. Resumable Session Model (Decision 2 = accepted)

The ego backend is **resumable** across CLI invocations (required for human–AI collaboration). Playwright remains one-shot.

- `fbu run '<url>' --goal '...' --browser ego --group team-alpha`:
  - Creates/reuses a task space under group `team-alpha`; prints and persists `spaceId` to the `--trace` file.
  - Runs the loop. On `done` it `finish()`es and closes the space (task complete). On `blocked` or `handoff` it exits but **keeps the space** for `fbu resume` (no `finish()`); the terminal status and reason are persisted to the trace `ego` block.
- `fbu resume <spaceId> --browser ego --group team-alpha`:
  - Calls `takeOverTaskSpace(spaceId)`, resolves the active tab's label (adopting if unmanaged — the human may have switched/opened a tab ≠ `p1`), and continues the agent loop from the human's current page using that label.
  - Convenience alias: `fbu run --resume [<trace>]` reads `spaceId` and `page_label` from the trace file's `ego` block (default trace path if omitted) instead of taking `spaceId` as a positional argument. Both entry points are equivalent.

The trace JSON gains an `ego` block: `{space_id, group, profile_id, server_name, page_label, terminal_status, handed_off, handoff_reason?}` (`terminal_status` ∈ `done`/`blocked`/`handoff`; `handoff_reason` set iff `handed_off`).

## 7. Human–AI Handoff (Decision 3 = A)

The model never selects a synthetic `HANDOFF` action — that would violate fbu's zero-hallucination principle (the model only chooses among observed DOM elements). Handoff is **triggered by the harness**, never by the model.

Triggers (gated by `FBU_EGO_HANDOFF=auto` default; `never` disables; `always` hands off at the first observe):
1. **Heuristic detection** during `observe`/`prepare`: lightweight patterns over `page["text"]`/`url`/`actions` indicating a login form (password field + submit), a captcha (iframe/signatures), a 2FA challenge, or an auth-redirect to an IdP. The detector is a small, explicit, overridable module (no model call) so it stays transparent and testable.
2. **`BLOCKED` status** from the agent loop (e.g., 3 repeated no-change actions) → handoff with reason `blocked-no-progress`.

On trigger:
- `agent.py` sets `state["status"] = "handoff"` (new status), records `{reason, url, elapsed_ms}` in a new `state["handoffs"]` list.
- `EgoBrowser.handoff(reason)` runs `await task.handOff()`, reads the current url via `page.info()`/`page.url()`, and returns `{reason, url}` to the agent (which records it in `state["handoffs"]`).
- The run ends with the trace carrying `spaceId` + handoff reason; the CLI prints a human instruction:
  > `[ego] Handoff (reason: login required). Complete the step in the ego lite browser, then run: fbu resume <spaceId> --browser ego --group <group>`
- `fbu resume` → `takeOverTaskSpace` → continue.

Handoff is an ego-only capability; `PlaywrightBrowser.handoff()` raises `NotImplementedError` (Playwright cannot persist a session for resume).

## 8. Data Flow (resumable + handoff)

```
fbu run --browser ego --group g1
  └─ EgoBrowser.observe (invocation A) → page dict
       └─ agent.predict (model ~70ms) / agent.act
            └─ EgoBrowser.act (invocation B) → new page dict
                 └─ if handoff trigger: status=handoff → EgoBrowser.handoff(reason) → exit, keep space
fbu resume <spaceId> --browser ego --group g1
  └─ EgoBrowser.takeover → takeOverTaskSpace → userPage → fresh page dict
       └─ continue agent loop from current page
```

## 9. CLI Surface

`fast_browser_use/cli.py` gains:

- Global/`run` args: `--browser {playwright,ego}` (default `playwright`, mirrors `FBU_BROWSER`), `--group <name>` (ego), `--profile <id>` (ego, advanced), `--handoff {auto,never,always}` (ego, default `auto`).
- New subcommand `resume`: `fbu resume <spaceId> --browser ego --group <name>` (also `--resume` on `run` to read `spaceId` from a trace file).
- `record` and `serve` keep Playwright-only semantics; `--browser ego` there raises a clear error (ego backend does not support deterministic headless recording — it depends on the running desktop app).

`fbu install-browser` stays Playwright-only; a `fbu doctor`-style preflight for ego (`ego lite` running? `ego-browser` on PATH? group service connectable?) is added to give actionable errors.

## 10. Error Handling

| Failure | Behavior |
| --- | --- |
| ego lite app not running / `ego-browser` not on PATH | `EgoBrowser` construction preflight fails fast with the install.md remediation link |
| `taskSpace`/`takeOverTaskSpace` rejected (user didn't approve) | Surface the ego message; status `blocked`; do not retry/route around (per ego Skill) |
| Subprocess returns non-JSON / nodejs exits non-zero | Treat like `StalePage` where recoverable (re-observe once); else `blocked` with the raw stderr in the trace |
| `page.cdp("Runtime.evaluate")` reports `exceptionDetails` | Map to existing `StalePage("Document changed during evaluation")` — identical to Playwright path |
| Handoff requested on Playwright backend | `NotImplementedError` with a message pointing to `--browser ego` |
| `act` select: option disappeared / not confirmed (`Runtime.evaluate` returns null) | `RuntimeError("Dropdown execution was not confirmed; inspect before retry.")` — identical to Playwright `browser_operation` (preserves the existing select-retry contract) |
| `act` select: evaluate interrupted mid-dispatch (`exceptionDetails`) | `RuntimeError("Dropdown execution was interrupted; inspect before retry.")` — identical to Playwright |
| ego `close(video_path=…)` with a non-None video_path | `ValueError("ego backend cannot record Playwright video; use --browser playwright for fbu record")` — ego has no Playwright video path |
| ego receives `viewport`/`headless` kwargs | silently ignored (ego lite owns its window; always headed) — the `make_browser` factory drops them before construction (§4.1) |

`StalePage` semantics are reused unchanged: ego's evaluate exceptions and navigation-during-evaluate produce the same `StalePage` the agent already handles (re-observe, bounded retries).

## 11. Testing Strategy

- `test_browser.py` — existing; verify `PlaywrightBrowser` rename keeps behavior (no regressions).
- `test_ego_backend.py` (new):
  - Unit: JS template generation (snapshot.js injection, guard expression per action kind, settle-loop boundaries) — pure string construction, no live browser.
  - Unit: handoff detector (login/captcha/2FA patterns) over fixture `page` dicts.
  - Unit: group→server-name/profile mapping; trace `ego` block round-trip.
  - Integration (marked `@pytest.mark.ego`, skipped unless `FBU_EGO_LIVE=1` and app running): real `ego-browser nodejs` round-trip — observe→act→re-observe on `example.com`, plus a handoff→resume sequence. These reproduce the §2.1 empirical checks as regression tests.
- `test_cli.py` — `--browser`, `--group`, `resume` arg parsing and the preflight error path.
- `test_agent.py` — new `handoff` status handling; ensure Playwright path still one-shot.

## 12. Non-Goals

- Replacing Playwright as the default or only backend. (ego is opt-in.)
- Reversing the latency cost (sub-second per step on ego) — accepted; not solvable without an undocumented direct-IPC path that would violate the zero-black-box principle.
- Linux/Windows/headless support for the ego backend — ego lite is a macOS desktop app; the ego backend is macOS-only by construction. Playwright covers the other platforms.
- Model-driven handoff — explicitly rejected (breaks zero-hallucination).
- New profiles created programmatically — `FBU_EGO_PROFILE` references existing profile ids only; profile creation stays in the ego lite app UI.

## 13. Compatibility & Migration

- Default `FBU_BROWSER=playwright`: zero change to existing runs, benchmarks, `fbu record`, SKILL, CI.
- The `Browser` rename is the only refactor to existing code; `agent.py` is unchanged at the call sites (same method names, same page-dict contract).
- OpenWiki / SKILL docs get a short "Optional ego backend" section noting the macOS-only, login-state-reuse, resumable-session, and ~2–3 s/step caveats.
- No dependency additions for the default path; the ego path requires only `ego-browser` on PATH (external CLI), keeping `pyproject.toml` unchanged.
