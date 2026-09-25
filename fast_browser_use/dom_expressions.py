"""Shared DOM/JS expressions and helpers used by every browser backend.

snapshot.js is the page-dict producer (READ_STATE); these are the smaller
expressions the driver interpolates node ids / action JSON into for
freshness guards and act dispatch. Both PlaywrightBrowser and EgoBrowser
import these so DOM semantics stay byte-identical across backends.
"""
import hashlib
import json
from pathlib import Path

READ_STATE = Path(__file__).with_name("snapshot.js").read_text()

# Cheap page-identity probe: returns the observed marker or None if the document navigated.
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"


def fingerprint(state):
    """sha256 over url/text/actions/scroll — order-independent (sort_keys)."""
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


_SELECT_ERRORS = {
    "not_confirmed": "Dropdown execution was not confirmed; inspect before retrying.",
    "interrupted": "Dropdown execution was interrupted; inspect before retrying.",
}


def select_runtime_error(kind):
    return RuntimeError(_SELECT_ERRORS[kind])


# Bounded wait after fill for combobox autocomplete options (used by observe).
AFTER_INPUT_WAIT = """(action => new Promise(resolve => {
  const field=window.__fastBrowserUse?.nodes.get(action.node);
  const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
  let frames=0, stopped=false;
  const finish=()=>{stopped=true;resolve()};
  setTimeout(finish,autocomplete ? 200 : 50);
  const ready=()=>{
    if (stopped) return;
    const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
      .split(/\\s+/).filter(Boolean);
    const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
    const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
    if (++frames>=2 && (!autocomplete || options.some(e=>{
      const r=e.getBoundingClientRect();
      return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
        e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
    }))) finish();
    else requestAnimationFrame(ready);
  };
  requestAnimationFrame(ready);
}))"""


def after_input_wait(action_json):
    return AFTER_INPUT_WAIT + "(" + action_json + ")"


def scroll_fresh_guard(node):
    return (
        f"(() => {{ const c=window.__fastBrowserUse; "
        f"return c ? [c.pageKey(),c.scrollGuard(c.nodes.get({node}))] : null; }})()"
    )


def click_fresh_guard(node):
    return (
        f"(() => {{ const c=window.__fastBrowserUse; "
        f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
    )


def scroll_point(node):
    inner = f"c.nodes.get({node})" if node is not None else "document.scrollingElement"
    return f"(() => {{ const c=window.__fastBrowserUse; return c?.scrollPoint({inner}); }})()"


# Act-dispatch guard: resolve center coords; select sets value + dispatches input/change in-page.
ACT_DISPATCH_GUARD = """(action => {
  const e=window.__fastBrowserUse?.nodes.get(action.node);
  if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
      !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
  if (e.tagName==='LABEL' && (!e.control || e.control.matches(':disabled') ||
      e.control.closest('[aria-disabled="true"]'))) return null;
  if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
  const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
  if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
  if (!e.contains(document.elementFromPoint(x,y))) return null;
  if (action.kind==='select') {
    if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
        !o.disabled && !o.closest('optgroup[disabled]'))) return null;
    e.value=action.value;
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
  }
  return {x,y};
})"""


def act_dispatch_guard(action_json):
    return ACT_DISPATCH_GUARD + "(" + action_json + ")"
