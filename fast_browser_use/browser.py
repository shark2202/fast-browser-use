"""Isolated Chromium with guarded DOM actions, adapted from Jev Ultrafast."""

import json
import os
import sys
import time
from typing import Protocol

from playwright.sync_api import sync_playwright

from .dom_expressions import (
    MARKER,
    READ_STATE,
    act_dispatch_guard,
    after_input_wait,
    click_fresh_guard,
    fingerprint,
    scroll_fresh_guard,
    scroll_point,
    select_runtime_error,
)


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class PlaywrightBrowser:
    def __init__(self, url, *, video_dir=None, viewport=None, headless=None, profile_dir=None, handoff_mode="auto"):
        self.driver = sync_playwright().start()
        self.chrome = None
        self.profile_dir = profile_dir
        self.handoff_mode = handoff_mode
        try:
            self.headless = os.environ.get("FBU_HEADLESS", "1") != "0" if headless is None else headless
            self.locale = os.environ.get("FBU_LOCALE", "en-US")
            options = {
                "viewport": viewport or {"width": 1120, "height": 780},
                "device_scale_factor": 1, "locale": self.locale,
            }
            if video_dir:
                options.update(record_video_dir=str(video_dir), record_video_size=options["viewport"])
            if profile_dir:
                os.makedirs(profile_dir, exist_ok=True)
                # launch_persistent_context returns a context that owns the profile dir and an initial page.
                self.chrome = self.driver.chromium.launch_persistent_context(
                    profile_dir, headless=self.headless, **options
                )
                self.context = self.chrome
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                self.target = "persistent-chromium"
            else:
                self.chrome = self.driver.chromium.launch(headless=self.headless)
                self.context = self.chrome.new_context(**options)
                self.page = self.context.new_page()
                self.target = "isolated-chromium"
            self.navigations = []
            self.page.on("request", self._navigation_request)
            self.video = self.page.video
            self.session = self.context.new_cdp_session(self.page)
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            self.close()
            raise

    @property
    def session_id(self):
        return self.profile_dir

    def handoff(self, reason, *, mechanism="auto"):
        """Yield control. mechanism: auto (resume if profile_dir else pause) | resume | pause."""
        eff = mechanism if mechanism != "auto" else self.handoff_mode
        if eff == "auto":
            eff = "resume" if self.profile_dir else "pause"
        if eff == "resume":
            if not self.profile_dir:
                raise ValueError("isolated context cannot survive process exit; use pause or --profile-dir")
            url = self.evaluate("location.href")
            self.close()  # close the context; the profile dir persists on disk for fbu resume
            return {"reason": reason, "url": url, "mechanism": "resume"}
        if eff == "pause":
            raise NotImplementedError("pause handoff lands in P3")
        raise ValueError(f"unknown handoff mechanism {eff!r}")

    def takeover(self):
        """Resume control. Persistent: __init__ already restored the profile; observe the current page."""
        if not self.profile_dir:
            raise ValueError("takeover requires persistent mode (profile_dir)")
        return self.observe()

    def call(self, method, **params):
        return self.session.send(method, params)

    def _navigation_request(self, request):
        if request.is_navigation_request() and request.frame == self.page.main_frame:
            self.navigations.append({"url": request.url, "method": request.method, "time": time.time()})

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression=after_input_wait(json.dumps(action)),
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        # Live sites can redirect for longer than a few animation frames. Retry only
        # this read; input has already been recorded and must never be replayed.
        for attempt in range(600):
            try:
                state = browser_operation({"operation": "observe", "session": self.session, "screenshot": screenshot})
                if not state["text"].strip() and not any(a.get("node") for a in state["actions"]) and attempt < 599:
                    raise StalePage("Waiting for the initial document to render")
                return state
            except StalePage:
                if attempt == 599:
                    raise
                time.sleep(0.05)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] == "scroll" and action.get("node") is not None:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(scroll_fresh_guard(node))
            return current == [page["page_key"], action["scroll_state"]]
        if action is not None and action["kind"] in {"click", "select", "fill"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(click_fresh_guard(node))
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def prepare(self, page, *, screenshot=False):
        """Bounded, read-only settling before inference; never weaken dispatch guards.

        Compare the observed semantic state, not arbitrary DOM mutations (e.g. a
        recording clock). Wait a little longer after filling so debounced search
        suggestions can appear, including searchboxes without role=combobox.
        """
        quiet_ms = int(os.environ.get("FBU_SETTLE_MS", "150"))
        if not 0 <= quiet_ms <= 2000:
            raise ValueError("FBU_SETTLE_MS must be between 0 and 2000")
        if quiet_ms == 0:
            return page
        started = changed = time.perf_counter()
        minimum = max(quiet_ms / 1000, 0.3 if getattr(self, "last_input_kind", None) == "fill" else 0)
        document = page["page_key"][0]
        if document != getattr(self, "prepared_document", None):
            # DOMContentLoaded can precede hydration of menus and controls. This
            # bounded window is inside the task timer, including the first page.
            minimum = max(minimum, 0.5)
        self.prepared_document = document
        self.last_input_kind = None
        deadline = started + max(1.5, minimum * 4)
        previous, samples, settled = page.get("marker"), 0, False
        while True:
            try:
                observed = browser_operation({"operation": "observe", "session": self.session, "screenshot": False})
            except StalePage:
                # Only the read is repeated. Any input was already logged.
                previous, changed = None, time.perf_counter()
            else:
                page = observed
                samples += 1
                now = time.perf_counter()
                has_content = bool(page["text"].strip()) or any(a.get("node") for a in page["actions"])
                ready = page.get("ready", True) and has_content
                if page["marker"] != previous or not ready:
                    previous, changed = page["marker"], now
                if ready and now - changed >= quiet_ms / 1000 and now - started >= minimum:
                    settled = True
                    break
            if time.perf_counter() >= deadline:
                break
            time.sleep(0.05)
        if not samples:
            raise StalePage("Document changed throughout preparation; observe again before inference")
        if screenshot:
            page["screenshot"] = self.call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
        page["preparation"] = {
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "samples": samples,
            "settled": settled,
        }
        return page

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        self.last_input_kind = action["kind"]
        return result

    def close(self, *, video_path=None):
        try:
            if self.chrome is None:
                return
            try:
                if self.profile_dir:
                    # persistent: chrome is the context; closing finalizes the profile and video.
                    self.chrome.close()
                else:
                    # isolated: chrome is a Browser; close the context first (finalizes video), then the browser.
                    if getattr(self, "context", None):
                        self.context.close()
                if video_path and getattr(self, "video", None):
                    self.video.save_as(str(video_path))
            finally:
                if not self.profile_dir:
                    self.chrome.close()
                self.chrome = None
        finally:
            self.driver.stop()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return session.send(method, params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise select_runtime_error("interrupted")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            node = action.get("node")
            if node is not None and type(node) is not int:
                raise ValueError("Invalid observed scroll region")
            target = evaluate(scroll_point(node))
            if target is None:
                raise StalePage("Scroll region is covered or no longer available")
            call("Input.dispatchMouseEvent", type="mouseWheel", **target, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate(act_dispatch_guard(json.dumps(action)))
            if target is None:
                if kind == "select":
                    raise select_runtime_error("not_confirmed")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info


class Browser(Protocol):
    """Backend contract. PlaywrightBrowser (isolated/persistent) and EgoBrowser implement this.

    handoff/takeover/session_id are added in P2/P3 as collaboration lands.
    """

    def observe(self, screenshot: bool = True) -> dict: ...
    def fresh(self, page: dict, action: dict | None = None) -> bool: ...
    def prepare(self, page: dict, *, screenshot: bool = False) -> dict: ...
    def act(self, action: dict, page: dict, text: str | None = None) -> dict: ...
    def close(self, *, video_path: str | None = None) -> None: ...


def make_browser(url, *, browser=None, **opts) -> Browser:
    """Select and construct a backend. FBU_BROWSER env (default 'playwright')."""
    browser = browser or os.environ.get("FBU_BROWSER", "playwright")
    if browser == "playwright":
        return PlaywrightBrowser(url, **_pw_opts(opts))
    if browser == "ego":
        from .ego_browser import EgoBrowser
        return EgoBrowser(url, **_ego_opts(opts))
    raise ValueError(f"FBU_BROWSER must be playwright or ego, got {browser!r}")


def _pw_opts(opts):
    allowed = {"video_dir", "viewport", "headless", "profile_dir", "handoff_mode"}
    return {k: v for k, v in opts.items() if k in allowed}


def _ego_opts(opts):
    allowed = {"group", "profile_id", "space_id", "handoff_mode"}
    return {k: v for k, v in opts.items() if k in allowed}
