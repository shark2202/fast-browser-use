"""macOS-only ego-browser backend.

Drives `ego-browser nodejs` via page.cdp()/page.evaluate(). Two invocations per
agent step (observe+prepare; then act+re-observe). Substitutes page.keyboard.
insertText for raw Input.insertText CDP (POC #5 — Input.insertText times out).

Scripts are built as lists of JS statements joined by "\\n" (avoids embedding
brace-heavy JS into .format() templates and avoids \\n-in-literal escaping traps).
Guard/act JS comes from the shared dom_expressions module so DOM semantics stay
byte-identical to Playwright.
"""
import json
import os
import subprocess
import sys
import time

from .browser import StalePage
from .dom_expressions import (
    MARKER,
    act_dispatch_guard,
    click_fresh_guard,
    fingerprint,
    scroll_fresh_guard,
    scroll_point,
    select_runtime_error,
)

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "snapshot.js")


def MAC_MODIFIERS(platform=sys.platform):
    """SelectAll modifier: Cmd(4) on macOS, Ctrl(2) elsewhere."""
    return 4 if platform == "darwin" else 2


def _read_state_line(snapshot_path):
    return f"const READ_STATE = await fs.readFile({snapshot_path!r}, 'utf8');"


def observe_script(space_id, page_label, snapshot_path=SNAPSHOT_PATH):
    """JS for an observe invocation: resume space, eval READ_STATE, screenshot."""
    return "\n".join([
        "const fs = await import('node:fs/promises');",
        _read_state_line(snapshot_path),
        f"const task = await taskSpace({space_id});",
        f"const page = task.page({page_label!r});",
        "const r = await page.cdp('Runtime.evaluate', "
        "{expression: READ_STATE, returnByValue: true});",
        "const info = r && r.result && r.result.value;",
        "const shot = await page.cdp('Page.captureScreenshot', "
        "{format: 'jpeg', quality: 72});",
        "console.log(JSON.stringify({page: info, screenshot: shot && shot.data}));",
    ])


def setup_script(space_or_name, url, snapshot_path=SNAPSHOT_PATH):
    """Create/resume a task space, navigate p1 to url, observe. Returns space_id+page+screenshot."""
    return "\n".join([
        "const fs = await import('node:fs/promises');",
        _read_state_line(snapshot_path),
        f"const task = await taskSpace({json.dumps(space_or_name)});",
        "const page = task.page('p1');",
        f"await page.goto({json.dumps(url)}, {{waitUntil: 'domcontentloaded'}});",
        "const r = await page.cdp('Runtime.evaluate', "
        "{expression: READ_STATE, returnByValue: true});",
        "const info = r && r.result && r.result.value;",
        "const shot = await page.cdp('Page.captureScreenshot', "
        "{format: 'jpeg', quality: 72});",
        "console.log(JSON.stringify({space_id: task.spaceId, page: info, screenshot: shot && shot.data}));",
    ])


def fresh_script(space_id, page_label, action):
    """Evaluate the freshness guard for the given action branch; returns the compared value."""
    if action is None:
        expr = MARKER
    elif action["kind"] == "scroll" and action.get("node") is not None:
        expr = scroll_fresh_guard(action["node"])
    elif action["kind"] in {"click", "select", "fill"}:
        expr = click_fresh_guard(action["node"])
    else:
        expr = MARKER
    guard_eval = "const r = await page.cdp('Runtime.evaluate', {expression: " + expr + ", returnByValue: true});"
    return "\n".join([
        f"const task = await taskSpace({space_id});",
        f"const page = task.page({page_label!r});",
        guard_eval,
        "console.log(JSON.stringify({value: r && r.result && r.result.value}));",
    ])


def act_script(space_id, page_label, action, fill_text, snapshot_path=SNAPSHOT_PATH, platform=sys.platform):
    """JS for an act+re-observe invocation. Dispatch per Contract B; fill uses keyboard.insertText."""
    kind = action["kind"]
    mods = MAC_MODIFIERS(platform)
    lines = [
        "const fs = await import('node:fs/promises');",
        _read_state_line(snapshot_path),
        f"const task = await taskSpace({space_id});",
        f"const page = task.page({page_label!r});",
        f"const action = {json.dumps(action)};",
    ]
    if kind == "wait":
        lines.append("await new Promise(r => setTimeout(r, 100));")
    elif kind == "scroll":
        node = action.get("node")
        guard = scroll_point(node)
        lines.append(
            "const sp = await page.cdp('Runtime.evaluate', "
            "{expression: " + guard + ", returnByValue: true});"
        )
        lines.append("const t = sp && sp.result && sp.result.value;")
        lines.append("if (!t) throw new Error('scroll region gone');")
        lines.append(
            "await page.cdp('Input.dispatchMouseEvent', "
            "{type: 'mouseWheel', x: t.x, y: t.y, deltaX: 0, deltaY: action.delta});"
        )
    else:  # click / fill / select — guard resolves coords; select sets value in-page
        guard = act_dispatch_guard(json.dumps(action))
        lines.append(
            "const g = await page.cdp('Runtime.evaluate', "
            "{expression: " + guard + ", returnByValue: true});"
        )
        lines.append("const c = g && g.result && g.result.value;")
        lines.append("if (!c) throw new Error('target gone');")
        if kind != "select":  # select is done inside the guard; click/fill need mouse + (fill) text
            lines.append(
                "await page.cdp('Input.dispatchMouseEvent', "
                "{type: 'mousePressed', x: c.x, y: c.y, button: 'left', clickCount: 1});"
            )
            lines.append(
                "await page.cdp('Input.dispatchMouseEvent', "
                "{type: 'mouseReleased', x: c.x, y: c.y, button: 'left', clickCount: 1});"
            )
            if kind == "fill":
                lines.append(
                    f"await page.cdp('Input.dispatchKeyEvent', "
                    f"{{type: 'keyDown', key: 'a', code: 'KeyA', modifiers: {mods}, "
                    f"commands: ['selectAll']}});"
                )
                lines.append(
                    f"await page.cdp('Input.dispatchKeyEvent', "
                    f"{{type: 'keyUp', key: 'a', code: 'KeyA', modifiers: {mods}}});"
                )
                # NOT Input.insertText (POC #5: times out); use ego's keyboard.insertText
                lines.append(f"await page.keyboard.insertText({json.dumps(fill_text)});")
    lines.append(
        "const r = await page.cdp('Runtime.evaluate', "
        "{expression: READ_STATE, returnByValue: true});"
    )
    lines.append("console.log(JSON.stringify({page: r && r.result && r.result.value}));")
    return "\n".join(lines)


class EgoBrowser:
    """macOS-only ego-browser backend. Drives `ego-browser nodejs` per operation."""

    def __init__(self, url, *, group="default", profile_id=None, space_id=None,
                 handoff_mode="resume", server_name=None, space_name=None, **_):
        self.url = url
        self.group = group
        self.profile_id = profile_id
        self.space_id = space_id
        self.handoff_mode = handoff_mode
        # None = default service (sandbox-safe); --ego-server-name needs Full Access (POC #2).
        self.server_name = server_name
        self.page_label = "p1"
        self.space_or_name = space_id if space_id is not None else (space_name or f"fbu-{int(time.time() * 1000)}")
        self.after_input = None
        self.handed_off = False

    def _cmd(self, js):
        cmd = ["ego-browser"]
        if self.server_name:
            cmd += ["--ego-server-name", self.server_name]
        return cmd + ["nodejs", "-e", js]

    def _run(self, js):
        proc = subprocess.run(self._cmd(js), capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"ego-browser exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        # ego-browser routes console.log to stderr (not stdout); scan both streams for the JSON.
        for line in reversed((proc.stdout + "\n" + proc.stderr).splitlines()):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        raise RuntimeError(
            "ego-browser produced no JSON: "
            f"stdout={proc.stdout.strip()[:120]} stderr={proc.stderr.strip()[:120]}"
        )

    def observe(self, screenshot=True):
        if self.space_id is None:
            data = self._run(setup_script(self.space_or_name, self.url))
            self.space_id = data.get("space_id")
        else:
            data = self._run(observe_script(self.space_id, self.page_label))
        page = data.get("page") or {}
        page["fingerprint"] = fingerprint(page)
        if screenshot and data.get("screenshot"):
            page["screenshot"] = data["screenshot"]
        return page

    def fresh(self, page, action=None):
        if self.space_id is None:
            return False
        data = self._run(fresh_script(self.space_id, self.page_label, action))
        current = data.get("value")
        if action is None:
            return current == page["marker"]
        if action["kind"] == "scroll" and action.get("node") is not None:
            return current == [page["page_key"], action["scroll_state"]]
        if action["kind"] in {"click", "select", "fill"}:
            return current == [page["page_key"], page["guards"].get(str(action["node"]))]
        return current == page["marker"]

    def prepare(self, page, *, screenshot=False):
        quiet_ms = int(os.environ.get("FBU_SETTLE_MS", "150"))
        if quiet_ms == 0:
            return page
        started = time.perf_counter()
        deadline = started + max(1.5, quiet_ms / 1000 * 4)
        previous = page.get("marker")
        while time.perf_counter() < deadline:
            try:
                observed = self.observe(screenshot=False)
            except Exception:
                return page
            if observed.get("marker") == previous and observed.get("ready", True):
                page = observed
                break
            previous = observed.get("marker")
            page = observed
            time.sleep(0.1)
        page["preparation"] = {
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "samples": 1,
            "settled": True,
        }
        return page

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
            self.after_input = None
            return {"executed": action["id"]}
        js = act_script(self.space_id, self.page_label, action, text, platform=sys.platform)
        try:
            data = self._run(js)
        except RuntimeError as exc:
            msg = str(exc)
            if "target gone" in msg or "scroll region gone" in msg:
                raise StalePage("Target changed or is covered. Observe again.")
            if "not confirmed" in msg:
                raise select_runtime_error("not_confirmed")
            raise
        self.after_input = action if action["kind"] != "wait" else None
        return {"executed": action["id"], "page": data.get("page")}

    def handoff(self, reason, *, mechanism="auto"):
        eff = mechanism if mechanism != "auto" else self.handoff_mode
        if eff != "resume":
            raise ValueError("ego handoff is resume-only (native task.handOff/takeOverTaskSpace)")
        url = self._url()
        self._run(
            f"const task = await taskSpace({self.space_id}); "
            "await task.handOff(); "
            "console.log(JSON.stringify({handed_off: true}));"
        )
        self.handed_off = True
        return {"reason": reason, "url": url, "mechanism": "resume"}

    def _url(self):
        data = self._run(
            f"const task = await taskSpace({self.space_id}); "
            f"const page = task.page({self.page_label!r}); "
            "console.log(JSON.stringify({url: await page.url()}));"
        )
        return data.get("url")

    def takeover(self):
        data = self._run(
            f"const task = await takeOverTaskSpace({self.space_id}); "
            "const up = task.userPage(); "
            "console.log(JSON.stringify({page_label: up.label}));"
        )
        self.page_label = data.get("page_label") or self.page_label
        return self.observe()

    def close(self, *, video_path=None):
        if self.space_id is not None and not self.handed_off:
            try:
                self._run(
                    f"const task = await taskSpace({self.space_id}); "
                    "await task.finish({keep: []}); "
                    "console.log(JSON.stringify({finished: true}));"
                )
            except Exception:
                pass
        self.space_id = None
