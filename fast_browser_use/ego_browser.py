"""macOS-only ego-browser backend.

Drives `ego-browser nodejs` via page.cdp()/page.evaluate(). Two invocations per
agent step (observe+prepare; then act+re-observe). Substitutes page.keyboard.
insertText for raw Input.insertText CDP (POC #5 — Input.insertText times out).

Scripts read snapshot.js at runtime via node:fs (avoids embedding ~17KB of
brace-heavy JS into a .format() template). Guard/act JS comes from the shared
dom_expressions module so DOM semantics stay byte-identical to Playwright.
"""
import json
import os
import sys

from .dom_expressions import act_dispatch_guard, scroll_point

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "snapshot.js")


def MAC_MODIFIERS(platform=sys.platform):
    """SelectAll modifier: Cmd(4) on macOS, Ctrl(2) elsewhere."""
    return 4 if platform == "darwin" else 2


def observe_script(space_id, page_label, snapshot_path=SNAPSHOT_PATH):
    """JS for an observe+prepare invocation: resume space, eval READ_STATE, screenshot."""
    return (
        "const fs = await import('node:fs/promises');\n"
        f"const READ_STATE = await fs.readFile({snapshot_path!r}, 'utf8');\n"
        f"const task = await taskSpace({space_id});\n"
        f"const page = task.page({page_label!r});\n"
        "const r = await page.cdp('Runtime.evaluate', "
        "{expression: READ_STATE, returnByValue: true});\n"
        "const info = r && r.result && r.result.value;\n"
        "const shot = await page.cdp('Page.captureScreenshot', "
        "{format: 'jpeg', quality: 72});\n"
        "console.log(JSON.stringify({page: info, screenshot: shot && shot.data}));\n"
    )


def act_script(space_id, page_label, action, fill_text, snapshot_path=SNAPSHOT_PATH, platform=sys.platform):
    """JS for an act+re-observe invocation. Dispatch per Contract B; fill uses keyboard.insertText."""
    kind = action["kind"]
    mods = MAC_MODIFIERS(platform)
    lines = [
        "const fs = await import('node:fs/promises');\n",
        f"const READ_STATE = await fs.readFile({snapshot_path!r}, 'utf8');\n",
        f"const task = await taskSpace({space_id});\n",
        f"const page = task.page({page_label!r});\n",
        f"const action = {json.dumps(action)};\n",
    ]
    if kind == "wait":
        lines.append("await new Promise(r => setTimeout(r, 100));\n")
    elif kind == "scroll":
        node = action.get("node")
        guard = scroll_point(node)
        lines.append(
            "const sp = await page.cdp('Runtime.evaluate', "
            "{expression: " + guard + ", returnByValue: true});\n"
            "const t = sp && sp.result && sp.result.value;\n"
            "if (!t) throw new Error('scroll region gone');\n"
            "await page.cdp('Input.dispatchMouseEvent', "
            "{type: 'mouseWheel', x: t.x, y: t.y, deltaX: 0, deltaY: action.delta});\n"
        )
    else:  # click / fill / select — guard resolves coords; select sets value in-page
        guard = act_dispatch_guard(json.dumps(action))
        lines.append(
            "const g = await page.cdp('Runtime.evaluate', "
            "{expression: " + guard + ", returnByValue: true});\n"
            "const c = g && g.result && g.result.value;\n"
            "if (!c) throw new Error('target gone');\n"
        )
        if kind != "select":  # select is done inside the guard; click/fill need mouse + (fill) text
            lines.append(
                "await page.cdp('Input.dispatchMouseEvent', "
                "{type: 'mousePressed', x: c.x, y: c.y, button: 'left', clickCount: 1});\n"
                "await page.cdp('Input.dispatchMouseEvent', "
                "{type: 'mouseReleased', x: c.x, y: c.y, button: 'left', clickCount: 1});\n"
            )
            if kind == "fill":
                lines.append(
                    f"await page.cdp('Input.dispatchKeyEvent', "
                    f"{{type: 'keyDown', key: 'a', code: 'KeyA', modifiers: {mods}, "
                    f"commands: ['selectAll']}});\n"
                    f"await page.cdp('Input.dispatchKeyEvent', "
                    f"{{type: 'keyUp', key: 'a', code: 'KeyA', modifiers: {mods}}});\n"
                    # NOT Input.insertText (POC #5: times out); use ego's keyboard.insertText
                    f"await page.keyboard.insertText({json.dumps(fill_text)});\n"
                )
    # re-observe after dispatch
    lines.append(
        "const r = await page.cdp('Runtime.evaluate', "
        "{expression: READ_STATE, returnByValue: true});\n"
        "console.log(JSON.stringify({page: r && r.result && r.result.value}));\n"
    )
    return "".join(lines)
