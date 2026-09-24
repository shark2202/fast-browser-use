# POC Findings: ego-browser Feasibility & Cross-Platform Browser-Backend Lock-In

Status: Complete (revised) · Date: 2026-09-25 · Drives: `docs/ego-backend-goal.md` + `docs/ego-backend-design.md` · Method: 9 empirical experiments on a running `ego lite` app + Playwright `launch`/`launch_persistent_context`.

## TL;DR

The original design treated ego-browser as **the** path for login-state reuse and human–AI collaboration, then (after POC 1–7) pivoted to a **three-tier** split. Experiments 8–9 (added after a design review) showed that split **over-fragmented**: the default isolated Playwright tier already supports **lightweight per-group isolation** (`storage_state` files) and **in-process-pause collaboration** (headed window + block + continue). The genuinely distinct capabilities are only **cross-process resume + full-profile reuse** (needs `launch_persistent_context`) and **native handOff/takeover UX** (ego only).

Locked model: **two backends** — `playwright` (cross-platform Win/macOS/Linux, with an `isolated` default mode and a `persistent` mode via `--profile-dir`) and `ego` (macOS-only advanced opt-in). ego cannot satisfy Windows (macOS-only build), so `playwright` is the cross-platform answer; its `persistent` mode covers full login reuse + cross-process resume, while the default `isolated` mode covers lightweight isolation + in-process-pause collaboration.

## Evidence Matrix (9 experiments)

| # | Hypothesis | Result | Evidence |
| --- | --- | --- | --- |
| 1 | ego lite ships a Windows build | **NO** | ego lite site Windows CTA = "Get pinged for Windows" (waitlist, no download); no Linux. `scripts/install.sh` line 216 `uname -s = Darwin` gate; DMG URLs macos-only. `index.js` `.exe` matches are acorn-parser `.exec()`. |
| 2 | ego `--ego-server-name` works in sandboxed hosts | **NO** | `Failed to connect to ego_cli bootstrap … from the default agent sandbox. Retry with Full Access.` Default service works: `SPACE_OK 5`. |
| 3 | ego nodejs hosts a long-lived TCP IPC server | **NO** | `net.createServer` script printed nothing (not even the first log), exit 0. Stdin streaming also yields empty `data`. **Persistent IPC blocked** → per-spawn (~0.5–1.2 s) unavoidable → ~2–3 s/step. |
| 4 | ego `page.cdp("Runtime.evaluate", READ_STATE)` produces fbu's full page-dict | **YES** | Ran fbu's `snapshot.js` via `page.cdp` on `benchmarks/pages/settings.html`: all **14/14** keys; 7 actions; marker/page_key arrays; guards object. **Contract A holds on ego.** |
| 5 | ego CDP Input domain covers all of fbu's act-dispatch | **MOSTLY** | `Input.dispatchMouseEvent`/`dispatchKeyEvent`/`Emulation.*` work. **`Input.insertText` → `CdpRequestTimeoutError`**; use ego `page.keyboard.insertText()`. Contract B shared except this one swap. |
| 6 | ego `page.evaluate` preserves `window.*` across calls | **YES** | Counter incremented across two `page.evaluate()` calls held its value. `window.__fastBrowserUse` cache pattern works on ego. |
| 7 | Playwright `launch_persistent_context` preserves login state across relaunch | **YES (cross-platform)** | Page-set cookie `fbu_plain` + `localStorage.fbu_ls` survived relaunch with the same `user_data_dir`. Win/macOS/Linux (Playwright-native). |
| 8 | Isolated `new_context(storage_state=<per-group-file>)` gives per-group isolation **without** a persistent dir | **YES** | POC-F: a fresh isolated context loaded `group_session` cookie + `localStorage.group=A` from a per-group file. Lightweight per-group workspace isolation on the default tier. |
| 9 | Isolated `launch(headless=False)` + in-process block supports human–AI collaboration **without** a persistent profile | **YES** | POC-G: headed window launched; the process can block for the human to act in the visible window, then continue the same process. In-process-pause collaboration on the default tier. |

## Possibilities Eliminated

- ❌ ego as a **Windows backend** — no Windows build (waitlist).
- ❌ ego **low-latency persistent IPC** — `net.createServer` blocked (POC #3); per-spawn unavoidable → ~2–3 s/step.
- ❌ ego **group isolation in sandboxed agents** — `--ego-server-name` needs Full Access (POC #2).
- ❌ **three-tier split** (isolated / persistent / ego as peers) — over-fragmented: the isolated tier already does lightweight isolation (POC #8) + in-process-pause collaboration (POC #9). `playwright-persistent` is not a separate tier; it is `playwright` + persistent mode.

## Possibilities Locked In

- ✅ **`playwright` backend, two modes**: `isolated` (default, `launch`+`new_context`, optional per-group `storage_state`) and `persistent` (`launch_persistent_context(user_data_dir)`, per-group dir) — cross-platform Win/macOS/Linux.
- ✅ **Per-group isolation on BOTH modes**: isolated = `storage_state` file (lightweight, POC #8); persistent = profile dir (full, POC #7).
- ✅ **Human–AI collaboration on BOTH modes**: in-process pause (headed + block, POC #9) on isolated **and** persistent; **cross-process resume** additionally on persistent (profile persists on disk) and ego (TaskSpace persists).
- ✅ ego runs fbu's `snapshot.js` + guards verbatim (Contract A); stays a **macOS-only** collaboration-UX opt-in with one Contract-B swap (`Input.insertText` → `keyboard.insertText`).

## Recommended Model: Two Backends + Profile Mode

| Backend | Platforms | Profile mode | Per-group isolation | Human–AI collaboration | Latency | Role |
| --- | --- | --- | --- | --- | --- | --- |
| `playwright` (default) | Win/macOS/Linux | `isolated` (`launch`+`new_context`) | `storage_state` file (lightweight, POC #8) | in-process pause (headed, POC #9) | ~0.5 s/step | benchmarks / CI / non-auth / lightweight auth |
| `playwright` + `--profile-dir` | Win/macOS/Linux | `persistent` (`launch_persistent_context`) | per-group profile dir (full, POC #7) | in-process pause **+ cross-process resume** | ~0.5 s/step | **primary for full login reuse + resume** |
| `ego` | macOS only | n/a | `--ego-server-name` (Full Access) / default-service task spaces | native `handOff`/`takeOverTaskSpace` | ~2–3 s/step | macOS premium collaboration UX |

### Group isolation (unified, cross-platform on playwright)
- `playwright` isolated: `~/.fbu/storage/<group>.json` per group (cookies + localStorage).
- `playwright` persistent: `~/.fbu/profiles/<group>/` per group (full profile).
- `ego`: `--ego-server-name=fbu-<group>` (Full Access) or default-service task spaces (sandbox fallback).

### Human–AI collaboration (unified `handoff` status + `fbu resume`)
- `playwright` (both modes): `--handoff-mode pause` (default on isolated) = headed window, block in-process, continue same process. `--handoff-mode resume` (default on persistent) = close context, profile retained, `fbu resume` relaunches same dir. (Isolated + resume = `ValueError`: ephemeral context cannot survive process exit.)
- `ego`: native `handOff()` / `takeOverTaskSpace(spaceId)`.

## Verdict

> Two backends, not three. `playwright` (cross-platform) with an `isolated` default mode and a `persistent` mode (selected by `--profile-dir`) covers Windows+macOS login reuse, per-group isolation, and human–AI collaboration — verified by POC #7/#8/#9. `ego` is a macOS-only advanced opt-in for native handOff/takeover UX, accepting ~2–3 s/step and Full-Access group isolation. fbu's shared `snapshot.js`/guards transfer to ego unchanged with one Contract-B swap (`Input.insertText` → `keyboard.insertText`).

## Open Items (to confirm before implementation plan)
- Real-profile reuse path: point `user_data_dir` at a **copy** of the user's Chrome/Edge profile (never the live profile — Chromium locks it). Verify portable login reuse from a real logged-in profile.
- Handoff detector heuristics (login wall / captcha / 2FA) and the in-process-pause signal mechanism (stdin line / file sentinel / timeout).
- Whether to ship `ego` in v1 or defer until macOS users request native handoff UX.
