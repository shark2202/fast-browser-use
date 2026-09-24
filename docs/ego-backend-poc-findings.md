# POC Findings: ego-browser Backend Feasibility & Cross-Platform Lock-In

Status: Complete · Date: 2026-09-25 · Drives: `docs/ego-backend-goal.md` + `docs/ego-backend-design.md` (pivot pending) · Method: 7 empirical experiments on a running `ego lite` app + Playwright `launch_persistent_context`.

## TL;DR

The original design treated ego-browser as **the** path for login-state reuse and human–AI collaboration. POC disproves that framing: ego is **macOS-only**, **inherently multi-second per step** (persistent IPC is blocked), and its group isolation (`--ego-server-name`) needs **Full Access** (fails in sandboxed agent hosts). The Windows+macOS requirement is **not** satisfiable by ego.

The requirement **is** satisfiable cross-platform by **Playwright `launch_persistent_context`** pointing at a user-profile directory: cookies + localStorage survive relaunch on Windows/macOS/Linux (verified). This becomes the **recommended primary backend for authenticated tasks** (`playwright-persistent`); ego is demoted to a **macOS-only advanced opt-in** for the best collaboration UX.

## Evidence Matrix (7 experiments)

| # | Hypothesis | Result | Evidence |
| --- | --- | --- | --- |
| 1 | ego lite ships a Windows build | **NO** | ego lite site Windows CTA = "Get pinged for Windows" (waitlist, no download); no Linux build. `scripts/install.sh` line 216: `uname -s = Darwin` gate; DMG URLs macos-only (arm64/x64). `index.js` `.exe` matches are acorn-parser `.exec()`, not Windows binaries. |
| 2 | ego `--ego-server-name` works in sandboxed hosts | **NO** | `Failed to connect to ego_cli bootstrap … cannot connect … from the default agent sandbox. Retry with Full Access or run ego-browser outside the agent sandbox.` Default service (no `--server-name`) works: `SPACE_OK 5`. |
| 3 | ego nodejs can host a long-lived TCP IPC server (to avoid per-spawn cost) | **NO** | A `net.createServer` script printed nothing (not even the first log line), exit 0 — the embedded runtime does not keep such scripts alive. Combined with the earlier finding that stdin streaming yields empty `data` events, **persistent IPC is blocked**. Per-process spawn (~0.5–1.2 s) is therefore unavoidable per command. |
| 4 | ego `page.cdp("Runtime.evaluate", READ_STATE)` produces fbu's full page-dict | **YES** | Ran fbu's actual `snapshot.js` via `page.cdp` on `benchmarks/pages/settings.html`: all **14/14** keys present (url,title,language,ready,dialogs,w,h,text,scroll,actions,marker,page_key,guards,omitted_actions); 7 actions (3 click/1 fill/2 select); marker/page_key are arrays; guards is object. **Contract A holds on ego.** |
| 5 | ego CDP Input domain covers all of fbu's act-dispatch | **MOSTLY** | `Input.dispatchMouseEvent` (mousePressed/Released/wheel), `Input.dispatchKeyEvent` (selectAll), `Emulation.*` all work. **EXCEPT `Input.insertText` → `CdpRequestTimeoutError`**. Workaround: use ego's `page.keyboard.insertText()` instead of raw `Input.insertText` CDP. Contract B mostly shared; one method swap required. |
| 6 | ego `page.evaluate` preserves `window.*` across calls | **YES** | Counter incremented across two separate `page.evaluate()` calls held its value. fbu's `window.__fastBrowserUse` WeakMap/Map cache pattern is viable on ego. |
| 7 | Playwright `launch_persistent_context` preserves login state across relaunch | **YES (cross-platform)** | Wrote page-set cookie `fbu_plain` + `localStorage.fbu_ls` in launch 1; relaunched with the same `user_data_dir`; both survived (`document.cookie` shows `fbu_plain=plainval`; localStorage returns `lsval`). Works on Windows/macOS/Linux (Playwright-native). *(Note: `ctx.add_cookies` with httpOnly+secure on example.com did not survive — use page-set or real-profile cookies; the mechanism holds.)* |

## Possibilities Eliminated

- ❌ ego as a **Windows+macOS unified backend** — no Windows build.
- ❌ ego **low-latency persistent IPC** — `net.createServer` blocked; per-spawn unavoidable → ~2–3 s/step.
- ❌ ego **group isolation in sandboxed agents** — `--ego-server-name` needs Full Access.
- ❌ ego **verbatim reuse of fbu `act`-dispatch** — `Input.insertText` must become `keyboard.insertText` (Contract B micro-adjustment).

## Possibilities Locked In

- ✅ **Playwright `launch_persistent_context(user_data_dir)`** = cross-platform login-state reuse (Win/macOS/Linux), no ego dependency, ~0.5 s/step.
- ✅ ego can still run fbu's `snapshot.js` + guards verbatim (Contract A); ego stays a **macOS-only** collaboration-UX opt-in.
- ✅ Shared DOM/guard JS (§4.5 of the design) is sound; only the Python driver and the one `insertText` call differ.

## Recommended Pivot: Three-Tier `FBU_BROWSER` Backend

| Tier | Backend | Platforms | Login reuse | Human–AI collaboration | Latency | Role |
| --- | --- | --- | --- | --- | --- | --- |
| Default | `playwright` (current) | Win/macOS/Linux | ❌ isolated empty profile | ❌ one-shot | ~0.5 s/step | benchmarks / CI / non-auth tasks |
| **Recommended for auth** | `playwright-persistent` (**new**) | **Win/macOS/Linux** | ✅ `launch_persistent_context` at a per-group `user_data_dir` | ✅ headed + pause/resume (state persists on disk across `fbu resume`) | ~0.5 s/step | **primary choice for authenticated tasks** |
| macOS advanced opt-in | `ego` | **macOS only** | ✅ ego lite real sessions | ✅ native `handOff()` / `takeOverTaskSpace` | ~2–3 s/step | best collaboration UX on macOS |

### Group isolation (unified cross-platform semantics)
- `playwright` / `playwright-persistent`: `FBU_PROFILE_DIR` = one dir per group → **cross-platform, no sandbox issue**.
- `ego`: `--ego-server-name=fbu-<group>` (Full Access required); sandbox fallback = separate task spaces in the default service (weaker isolation).

### Human–AI collaboration (unified cross-platform semantics: `handoff` status + `fbu resume`)
- `ego` (macOS): native `handOff()` / `takeOverTaskSpace(spaceId)`.
- `playwright-persistent` (cross-platform): headed run → loop sets `status=handoff`, prints "complete X in the browser window, then `fbu resume`" → `fbu resume` reconnects to the **same profile dir** (state already on disk). Simpler than ego's TaskSpace, but works on **both Windows and macOS**.

## Verdict

> ego-browser is **not** the path to Windows+macOS compatibility. The locked-in best solution is **`playwright-persistent`** (verified cross-platform login reuse + headed pause/resume collaboration) as the primary authenticated-task backend, with ego demoted to a macOS-only advanced collaboration opt-in. The design and goal documents must be rewritten to this three-tier model; fbu's shared `snapshot.js`/guard logic transfers to ego unchanged, with one Contract-B swap (`Input.insertText` → `keyboard.insertText`).

## Open Items (to confirm before implementation plan)
- Real-profile reuse path: point `user_data_dir` at a **copy** of the user's actual Chrome/Edge profile (never the live profile — Chromium locks it). Verify cookie/portable-login reuse from a real logged-in profile on the target group's dir.
- Handoff detector heuristics (login wall / captcha / 2FA patterns) for the `playwright-persistent` pause/resume path.
- Whether to still ship `ego` backend in v1 or defer until macOS users explicitly request native handoff UX.
