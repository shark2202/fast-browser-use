# Goal: Two-Backend Browser with Profile Modes — Cross-Platform Login Reuse + Human–AI Collaboration

Status: Revised (POC-locked, 2-backend) · Date: 2026-09-25 · Realized by: `docs/ego-backend-design.md` · Evidence: `docs/ego-backend-poc-findings.md` · Project: fast-browser-use

## 1. Problem

`fbu` drives an **isolated, empty-profile Playwright Chromium**. Correct for benchmarks/CI, but it cannot reach the user's **real logged-in sessions**, has no **group isolation**, and no way for a human to step into a stuck loop and let the agent resume.

The initial idea was to adopt **ego-browser** as that login-reuse + collaboration backend. Empirical POC (9 experiments, see `docs/ego-backend-poc-findings.md`) disproved this framing and then refined it:

- **ego lite is macOS-only** (Windows is a waitlist; no Linux). ego **cannot** satisfy a Windows+macOS requirement.
- ego's group isolation (`--ego-server-name`) needs **Full Access** and fails in sandboxed agent hosts.
- ego cannot host persistent IPC → **~2–3 s/step** inherently.
- **However**: Playwright's own `launch_persistent_context` (full login reuse, POC #7) **and** the default isolated `new_context(storage_state=…)` (lightweight per-group isolation, POC #8) **and** headed in-process pause (collaboration, POC #9) are all cross-platform. So a three-tier split was over-fragmented; **two backends + a profile mode** suffice.

## 2. Objective

Ship **two browser backends** under one `FBU_BROWSER` axis, so the **same fbu** works on **Windows + macOS (+ Linux)** with login reuse, group isolation, and human–AI collaboration:

1. **`playwright`** (default, cross-platform Win/macOS/Linux) — one backend, **two profile modes**:
   - **`isolated`** (default): `launch()` + `new_context()`; optional per-group `storage_state` file for lightweight isolation; **in-process-pause** collaboration (headed + block + continue). Benchmarks / CI / lightweight auth.
   - **`persistent`** (via `--profile-dir`): `launch_persistent_context(user_data_dir)`; per-group profile dir for **full** login reuse; in-process pause **+ cross-process resume** (`fbu resume` relaunches the same dir). **Primary for authenticated tasks needing real login state or resume.**
2. **`ego`** (macOS-only advanced opt-in) — reuses ego lite's real sessions; native `handOff`/`takeOverTaskSpace`; ~2–3 s/step; needs Full Access for `--ego-server-name` group isolation.

fbu's `snapshot.js`, guards, `actions.py`, `verification.py`, and agent loop are **shared** across both backends and both playwright modes; only the driver and one CDP call (`Input.insertText` → ego `keyboard.insertText`) differ.

## 3. Success Criteria

A goal is met only when **all** hold, verified by evidence (tests + telemetry, building on the locked POC results):

1. **Default zero regression** — `FBU_BROWSER=playwright` (isolated, default) keeps existing `test_*`, `fbu record`, and the Qwen3.5-9B Wikipedia demo trace byte-equivalent in page-dict shape and pass/fail vs `main`.
2. **Persistent login reuse (cross-platform)** — on Windows + macOS, `--profile-dir` at a per-group dir persists page-set cookies + localStorage across `fbu` runs (POC #7); pointing it at a **copy** of the user's real Chrome/Edge profile yields an already-logged-in session without manual re-login.
3. **Isolated lightweight isolation (cross-platform)** — `--group` on the default isolated mode loads/saves a per-group `storage_state` file (cookies + localStorage), so two groups never share sessions (POC #8).
4. **Human–AI collaboration (cross-platform)** — on `playwright` both modes, a run reaches `status=handoff`, opens/keeps a **headed** window, and (pause mode) blocks in-process until the human signals then continues — on Windows **and** macOS (POC #9). In persistent+resume mode, the run exits retaining the profile; `fbu resume` relaunches the same dir and continues.
5. **`ego` macOS-only opt-in** — runs fbu's `snapshot.js` verbatim producing all 16 Contract-A keys (POC #4); native `handOff`→`takeOverTaskSpace` resume; ~2–3 s/step documented; `--ego-server-name` group isolation requires Full Access (POC #2), with a sandbox fallback (separate default-service task spaces).
6. **One axis, one factory** — `FBU_BROWSER` selects the backend via `make_browser()`; `--profile-dir` selects the playwright mode; `FBU_BACKEND` (inference) stays orthogonal. Contract A identical across backends/modes; Contract B shared except the ego `Input.insertText` → `keyboard.insertText` substitution.
7. **Zero new dependencies** for both `playwright` modes; the `ego` path requires only the external `ego-browser` CLI on PATH plus a running `ego lite` app (macOS). `pyproject.toml` unchanged.
8. **Platform honesty** — `ego` is macOS-only and the preflight rejects it elsewhere with an actionable message; `playwright` covers Windows/macOS/Linux.

## 4. Scope

**In scope**
- `Browser` protocol + `make_browser()` factory; rename `browser.py` → `PlaywrightBrowser` with **`isolated`/`persistent` modes** (one class, `profile_dir` selects mode); new `EgoBrowser` (macOS opt-in).
- Shared DOM/JS module (`dom_expressions.py`) for `snapshot.js` + the 6 guard/act expressions, imported by both backends.
- Per-group isolation: `storage_state` file (isolated) / profile dir (persistent) on playwright; `--ego-server-name` + optional `profileId` on ego.
- Harness-driven handoff (heuristic detector + `BLOCKED`), gated by `FBU_HANDOFF=auto|never|always`; two mechanisms — `pause` (in-process, both playwright modes) and `resume` (cross-process, persistent+ego); uniform `handoff` status + `fbu resume`.
- CLI: `--browser`, `--group`, `--profile-dir`, `--handoff`/`--handoff-mode`, `resume` subcommand, preflight.
- Tests: unit (templates, detector, mapping, trace) + gated live (`FBU_LIVE=1`, `FBU_EGO_LIVE=1`).

**Out of scope (non-goals)**
- ego as the Windows backend — no Windows build (waitlist). Deferred until ego lite ships Windows.
- Sub-second per-step latency on ego — accepted (persistent IPC blocked, POC #3).
- Reverse-engineering ego lite's named-service socket — no black-box IPC.
- Model-driven handoff — rejected (breaks zero-hallucination).
- Pointing `user_data_dir` at a **live** Chrome profile — Chromium locks it; use a copy or a dedicated dir.
- OpenWiki page edits — regenerated by the scheduled workflow.

## 5. Constraints

- **Zero-hallucination preserved** — the model chooses only among observed DOM elements; handoff is harness-triggered.
- **Guarded execution preserved** — `StalePage`, freshness guards, DONE-as-gate, `verification.py` checks run identically on both backends/modes.
- **Local-first preserved** — no cloud inference; both backends run locally.
- **No black-box IPC** — drive ego only via the documented `ego-browser nodejs` CLI + `page.cdp()`/`page.evaluate()`.
- **Profile safety** — persistent mode uses a copy/dedicated profile dir, never the live Chrome profile.

## 6. Stakeholders & Usage

- **End user (any platform)**: authenticated browser tasks with login reuse + handoff→resume. Windows and macOS both covered by `playwright` (persistent mode for full login, isolated for lightweight).
- **Team lead**: parallel isolated task groups via `--group` (storage_state file or profile dir per group), cross-platform.
- **macOS power user with ego lite**: opts into `ego` for premium native handoff UX when higher latency is acceptable.

## 7. Measurable Outcome

A single authenticated task completes via `fbu run --browser playwright --profile-dir ~/.fbu/profiles/personal --group personal` + (optionally) one `fbu resume`, with the human performing only the login step (or none, if the profile copy is already logged in), and the agent performing all navigation/form/verification — on **both Windows and macOS**. Telemetry records the backend, mode, group, profile dir / storage_state file, `handoffs[]` (mechanism + reason), and per-step latencies.
