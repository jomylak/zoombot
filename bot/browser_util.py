"""Frame-walking helpers shared by the deterministic joiner, the selector
store, and the AI agent. Split out of joiner.py so all three can use the
same logic without importing each other in a circle.
"""
import time


def content_frames(page):
    """The pre-join and in-meeting UI render inside a nested iframe whose URL
    carries per-session join tokens (wpk=, _x_zm_rtaid=, ...) -- never the
    top-level document. page.locator()/wait_for_selector() only search the
    main frame, so every lookup has to walk all frames instead. Skip
    reCAPTCHA and blank placeholder frames; they never hold anything we want
    and just slow every check down."""
    return [f for f in page.frames if "recaptcha" not in f.url and f.url != "about:blank"]


def first(page, selectors, timeout=3000):
    """Return the first selector that resolves in any content frame, else None."""
    for frame in content_frames(page):
        for sel in selectors:
            try:
                el = frame.wait_for_selector(sel, timeout=timeout, state="visible")
                if el:
                    return el
            except Exception:
                continue
    return None


def retry_for_any(page, selectors, timeout_s, poll_s=3, per_try_timeout=3000, log=None):
    """Keep re-scanning the whole selector list every few seconds until one
    resolves or timeout_s elapses -- a single first() pass gives up for good
    once it's cycled through the list, even if the field just hadn't rendered
    yet. Also rides through transient errors (mid re-render, brief overlay)
    that would otherwise abort a one-shot wait_for_selector call."""
    deadline = time.time() + timeout_s
    attempt = 0
    while True:
        attempt += 1
        try:
            el = first(page, selectors, timeout=per_try_timeout)
        except Exception as e:
            if log:
                log.info("retry attempt %d: transient error %s", attempt, e)
            el = None
        if el:
            return el
        if time.time() >= deadline:
            return None
        if log:
            log.info("retry attempt %d: none of %s found yet, retrying in %ds",
                     attempt, selectors, poll_s)
        page.wait_for_timeout(poll_s * 1000)


def visible(page, selectors) -> bool:
    for frame in content_frames(page):
        for sel in selectors:
            try:
                if frame.locator(sel).first.is_visible(timeout=1000):
                    return True
            except Exception:
                continue
    return False
