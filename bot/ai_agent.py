"""AI fallback for joining meetings on platforms we don't have selectors
for yet. Two modes:

- drive(): full takeover of the pre-join sequence on a brand-new platform
  (nothing learned, no baseline). Loops, calling the model each step with
  the current visible elements + a screenshot, executing whatever it asks
  for, and recording every selector that actually works.
- find_role(): a single, narrow "where is the element for this one role"
  call, used when selector_store.resolve() comes up empty for an
  otherwise-known variant (selector drift). Only locates -- the caller
  (joiner.py) performs the actual click/fill, same contract as resolve().

The model is never shown or asked to supply real values for name/passcode
fields -- it only names a role, and the code substitutes the real value.
It is also never given a path to interact with a password field; the
system prompt tells it to bail out (finish("needs_login")) the instant a
page asks for a personal sign-in instead of a guest join.
"""
import base64
import json
import logging
import time

import requests

from . import config, selector_store
from .browser_util import content_frames, first

log = logging.getLogger("ai_agent")

API_URL = "https://openrouter.ai/api/v1/chat/completions"

FILL_ROLES = {"name_input", "passcode_input"}

ELEMENT_SCAN_JS = """
() => {
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  const cssPath = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.getAttribute('data-testid'))
      return `[data-testid="${el.getAttribute('data-testid')}"]`;
    const parent = el.parentElement;
    if (!parent) return el.tagName.toLowerCase();
    const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
    const idx = siblings.indexOf(el) + 1;
    return cssPath(parent) + ' > ' + el.tagName.toLowerCase() + `:nth-of-type(${idx})`;
  };
  const out = [];
  document.querySelectorAll("button, input, a, [role=button], [role=textbox], textarea, [tabindex]")
    .forEach((el) => {
      if (!isVisible(el)) return;
      const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.placeholder || '')
        .trim().slice(0, 80);
      out.push({ selector: cssPath(el), tag: el.tagName.toLowerCase(),
                 type: el.getAttribute('type') || '', text });
    });
  return out.slice(0, 40);
}
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "act",
            "description": "Interact with one element on the page, identified "
                           "by an exact selector from the element list you were given.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "description": "What this element is for."},
                    "action": {"type": "string", "enum": ["click", "fill", "observe"]},
                    "selector": {"type": "string"},
                },
                "required": ["role", "action", "selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Wait for the page to settle, then look again.",
            "parameters": {
                "type": "object",
                "properties": {"seconds": {"type": "number", "maximum": 10}},
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Stop and report the outcome.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string",
                              "enum": ["in_meeting", "needs_login", "stuck"]},
                },
                "required": ["status"],
            },
        },
    },
]

RULES = """CRITICAL RULES -- never break these:
- Never type, click, or otherwise interact with a password field.
- The moment the page asks to sign in with a personal Microsoft, Google,
  Zoom, or SSO account -- rather than offering a guest/anonymous join --
  immediately call finish with status "needs_login". Do not try to work
  around it, do not guess at credentials, do not click "sign in" buttons
  to see what happens.
- For "fill" actions, only use role "name_input" or "passcode_input" --
  never type your own text; the real value is substituted for you based
  on the role you name.
- Only use selector strings exactly as given in the element list."""

SYSTEM_PROMPT_DRIVE = f"""You are driving a headless browser to join a video
meeting as a guest, using the tools provided. Look at the visible elements
and the screenshot each turn, then call exactly one tool. Your goal:
dismiss any device-permission prompts, enter the guest name where asked,
enter a passcode field if one is present, click through to the meeting,
and call finish("in_meeting") once you can see you're actually in the
call (meeting controls, participant list, etc. visible).

{RULES}"""

SYSTEM_PROMPT_FIND = f"""You are looking at a meeting-join web page. Find the
single element that matches the requested role: "{{role}}". Call act with
that role, action "observe", and its exact selector. If it isn't present,
or the only way forward is a personal sign-in wall, call
finish("needs_login") or finish("stuck") as appropriate -- do not guess.

{RULES}"""


def _scan(page):
    elements = []
    for frame in content_frames(page):
        try:
            found = frame.evaluate(ELEMENT_SCAN_JS)
        except Exception:
            continue
        elements.extend(found)
        if len(elements) >= 40:
            break
    return elements[:40]


def _screenshot_b64(page):
    try:
        return base64.b64encode(page.screenshot()).decode()
    except Exception:
        return None


def _user_content(page, prefix):
    elements = _scan(page)
    content = [{"type": "text", "text": f"{prefix}\nVisible elements (JSON):\n{json.dumps(elements)}"}]
    shot = _screenshot_b64(page)
    if shot:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot}"}})
    return content


def _call_model(messages):
    resp = requests.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.OPENROUTER_MODEL,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "required",
        },
        timeout=config.AGENT_STEP_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


def _execute(page, rec, role, action, selector):
    el = first(page, [selector], timeout=3000)
    if not el:
        return False, f"selector not found or not visible: {selector}"
    try:
        if action == "click":
            el.click()
        elif action == "fill":
            if role == "name_input":
                value = config.DISPLAY_NAME
            elif role == "passcode_input":
                value = rec.get("passcode") or ""
            else:
                return False, f"fill is only allowed for name_input/passcode_input, not {role}"
            if not value:
                return False, f"no value available to fill for role {role}"
            el.fill(value)
        return True, "ok"
    except Exception as e:
        return False, str(e)


def drive(page, variant_key, rec):
    """Full takeover of the pre-join sequence. Returns 'in_meeting',
    'needs_login', or 'stuck'."""
    if not config.AGENT_ENABLED:
        return "stuck"
    platform = variant_key.split(":", 1)[0]
    messages = [{"role": "system", "content": SYSTEM_PROMPT_DRIVE}]
    steps = 0
    deadline = time.time() + config.AGENT_MAX_STEPS * config.AGENT_STEP_TIMEOUT_S
    while steps < config.AGENT_MAX_STEPS and time.time() < deadline:
        steps += 1
        messages.append({"role": "user",
                         "content": _user_content(page, f"Step {steps}.")})
        try:
            message = _call_model(messages)
        except Exception as e:
            log.warning("agent call failed on step %d: %s", steps, e)
            return "stuck"
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            continue
        call = tool_calls[0]
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"])
        except Exception:
            args = {}

        if name == "finish":
            status = args.get("status", "stuck")
            log.info("agent finished after %d step(s): %s", steps, status)
            if status == "needs_login":
                selector_store.mark_needs_login(variant_key, platform, rec.get("join_url") or "")
            return status

        if name == "wait":
            page.wait_for_timeout(min(float(args.get("seconds", 2)), 10) * 1000)
            result_text = "waited"
        elif name == "act":
            role, action, selector = args.get("role"), args.get("action"), args.get("selector")
            ok, msg = _execute(page, rec, role, action, selector)
            if ok:
                selector_store.record(variant_key, role, selector)
            result_text = msg
        else:
            result_text = f"unknown tool {name}"

        messages.append({"role": "tool", "tool_call_id": call["id"], "content": result_text})

    log.warning("agent step/time budget exhausted driving %s", variant_key)
    return "stuck"


def find_role(page, variant_key, role, rec):
    """Locate (not act on) the element for one role. Returns a Playwright
    element handle or None -- same contract as selector_store.resolve()."""
    if not config.AGENT_ENABLED:
        return None
    platform = variant_key.split(":", 1)[0]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_FIND.format(role=role)},
        {"role": "user", "content": _user_content(page, f"Find the element for role '{role}'.")},
    ]
    try:
        message = _call_model(messages)
    except Exception as e:
        log.warning("agent find_role(%s) failed: %s", role, e)
        return None

    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        return None
    call = tool_calls[0]
    name = call["function"]["name"]
    try:
        args = json.loads(call["function"]["arguments"])
    except Exception:
        return None

    if name == "finish":
        status = args.get("status")
        log.info("agent find_role(%s): %s", role, status)
        if status == "needs_login":
            selector_store.mark_needs_login(variant_key, platform, rec.get("join_url") or "")
        return None
    if name != "act":
        return None

    selector = args.get("selector")
    el = first(page, [selector], timeout=3000)
    if not el:
        return None
    selector_store.record(variant_key, role, selector)
    log.info("agent found %s: %s", role, selector)
    return el
