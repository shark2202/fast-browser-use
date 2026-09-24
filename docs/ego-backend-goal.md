# Goal: Three-Tier Browser Backend — Cross-Platform Login Reuse + Human–AI Collaboration

Status: Revised (POC-locked) · Date: 2026-09-25 · Realized by: `docs/ego-backend-design.md` · Evidence: `docs/ego-backend-poc-findings.md` · Project: fast-browser-use

## 1. Problem

`fbu` drives an **isolated, empty-profile Playwright Chromium**. This is correct for headless benchmarks and CI, but it cannot reach the user's **real logged-in sessions**. Tasks requiring authentication (Google, GitHub, internal systems, paywalls, 2FA) are unreachable without manually recreating login state inside the isolated browser — defeating the purpose. There is also no notion of **group isolation** between concurrent task groups, and no way for a human to step into a stuck loop and have the agent resume cleanly.

The initial idea was to adopt **ego-browser** as that login-reuse + collaboration backend. Empirical POC (7 experiments, see `docs/ego-backend-poc-findings.md`) disproved this framing:

- **ego lite is macOS-only today** (Windows is a waitlist; no Linux build). ego **cannot** satisfy a Windows+macOS requirement.
- ego's group isolation (`--ego-server-name`) needs **Full Access** and fails inside sandboxed agent hosts.
- ego cannot host a persistent IPC server → per-step process startup is unavoidable → **~2–3 s/step** inherently.

## 2. Objective

Ship a **three-tier `FBU_BROWSER` backend** under one axis, so the **same fbu** works on **Windows + macOS (+ Linux)** with login reuse and human–AI collaboration, with ego available only as a macOS premium opt-in:

1. **`playwright`** (default, unchanged) — isolated empty profile; benchmarks / CI / non-auth tasks. Cross-platform.
2. **`playwright-persistent`** (NEW, primary for authenticated tasks) — `launch_persistent_context(user_data_dir)` pointing at a per-group profile directory; reuses real logged-in cookies/localStorage across runs; **headed pause/resume** for human–AI collaboration. **Cross-platform: Windows / macOS / Linux.**
3. **`ego`** (macOS-only advanced opt-in) — reuses ego lite's real sessions; native `handOff()`/`takeOverTaskSpace` collaboration; accepts ~2–3 s/step; needs Full Access for `--ego-server-name` group isolation.

fbu's `snapshot.js`, guards, `actions.py`, `verification.py`, and agent loop are **shared** across all three tiers; only the driver and one CDP call (`Input.insertText` → ego `keyboard.insertText`) differ.

## 3. Success Criteria

A goal is met only when **all** hold, verified by evidence (tests + telemetry, building on the locked POC results):

1. **Default zero regression** — `FBU_BROWSER=playwright` (default) keeps existing `test_*`, `fbu record`, and the Qwen3.5-9B Wikipedia demo trace byte-equivalent in page-dict shape and pass/fail vs `main`.
2. **`playwright-persistent` cross-platform login reuse** — on Windows + macOS, launching with a per-group `user_data_dir` persists page-set cookies + localStorage across `fbu` runs (POC-E2 verified the mechanism; the implementation test re-verifies on the target platform). Pointing `user_data_dir` at a **copy** of the user's real Chrome/Edge profile yields an already-logged-in session without manual re-login.
3. **`playwright-persistent` human–AI collaboration** — a run reaches `status=handoff`, prints the resume instruction, exits with the profile retained; after the human completes the step in the visible (headed) window, `fbu resume` reconnects to the **same profile dir** and continues — on **both Windows and macOS**.
4. **`ego` macOS-only opt-in** — runs fbu's `snapshot.js` verbatim producing all 16 Contract-A keys (POC-C verified 14 snapshot keys); native `handOff`→`takeOverTaskSpace` resume works; latency documented at ~2–3 s/step; `--ego-server-name` group isolation requires Full Access (documented; sandbox fallback = separate default-service task spaces).
5. **One axis, one factory** — `FBU_BROWSER` selects among the three tiers via a single `make_browser()` factory; `FBU_BACKEND` (inference) stays orthogonal. Contract A (page-dict) is identical across all tiers; Contract B (action dispatch) is shared except the ego `Input.insertText` → `keyboard.insertText` substitution.
6. **Group isolation, cross-platform semantics** — `playwright`/`playwright-persistent`: one `user_data_dir` per group (cross-platform, no sandbox issue); `ego`: `--ego-server-name=fbu-<group>` (Full Access) or default-service task spaces (sandbox). Two groups never observe each other's pages/cookies.
7. **Zero new dependencies** for the `playwright`/`playwright-persistent` paths; the `ego` path requires only the external `ego-browser` CLI on PATH plus a running `ego lite` app (macOS). `pyproject.toml` unchanged.
8. **Platform honesty** — `ego` is macOS-only and the CLI preflight rejects it elsewhere with an actionable message; `playwright`/`playwright-persistent` cover Windows/macOS/Linux.

## 4. Scope

**In scope**
- `Browser` protocol + `make_browser()` factory; rename `browser.py` → `PlaywrightBrowser`; **new `PlaywrightPersistentBrowser`** (the cross-platform login-reuse tier); new `EgoBrowser` (macOS opt-in).
- Shared DOM/JS module (`dom_expressions.py`) for `snapshot.js` + the 6 guard/act expressions, imported by all three tiers.
- Per-group `user_data_dir` profile isolation (cross-platform) for the persistent tier; `--ego-server-name` + optional `profileId` for ego.
- Harness-driven handoff (heuristic detector + `BLOCKED`), gated by `FBU_HANDOFF=auto|never|always`; uniform `handoff` status + `fbu resume`.
- CLI: `--browser`, `--group`, `--profile-dir`, `--handoff`, `resume` subcommand, preflight.
- Tests: unit (templates, detector, mapping, trace) + gated live integration (`FBU_LIVE=1` for ego, `FBU_PERSISTENT_LIVE=1` for persistent).

**Out of scope (non-goals)**
- ego as the Windows backend — no Windows build exists (waitlist). Deferred until ego lite ships Windows.
- Sub-second per-step latency on ego — accepted (persistent IPC blocked).
- Reverse-engineering ego lite's named-service socket — no black-box IPC.
- Model-driven handoff — rejected (breaks zero-hallucination).
- Pointing `user_data_dir` at a **live** (in-use) Chrome profile — Chromium locks it; the design uses a profile **copy** or a dedicated fbu profile dir.
- OpenWiki page edits — regenerated by the scheduled workflow; only source docs change.

## 5. Constraints

- **Zero-hallucination preserved** — the model continues to choose only among observed DOM elements; handoff is harness-triggered, never model-generated.
- **Guarded execution preserved** — `StalePage`, freshness guards, DONE-as-gate, and independent `verification.py` checks run identically on all three tiers.
- **Local-first preserved** — no cloud inference introduced; all three browser tiers run locally.
- **No black-box IPC** — drive ego only via the documented `ego-browser nodejs` CLI + `page.cdp()`/`page.evaluate()` escape hatches.
- **Profile safety** — persistent tier uses a copy/dedicated profile dir, never the user's live Chrome profile.

## 6. Stakeholders & Usage

- **End user (any platform)**: runs authenticated browser tasks locally with login reuse; collaborates via handoff→resume. Windows and macOS users both covered by `playwright-persistent`.
- **Team lead**: runs parallel isolated task groups per team via `--group` (one profile dir per group), cross-platform.
- **macOS power user with ego lite**: opts into `ego` for the premium native handoff UX when the higher latency is acceptable.

## 7. Measurable Outcome

A single authenticated task (e.g., "save a setting on a site the user is logged into") completes via `fbu run --browser playwright-persistent --group personal` + one `fbu resume`, with the human performing only the login step (or none, if the profile copy is already logged in), and the agent performing all navigation/form/verification — on **both Windows and macOS**. Telemetry records the tier, group, profile dir, `handoffs[]`, and per-step latencies.
