"""Resolves a role (name_input, join_button, ...) to a live page element for
a given variant_key, trying learned selectors first, then any hand-written
baseline for the platform. Pure DOM lookups -- no agent calls here. When
this comes back empty, the caller (joiner.py) is the one that decides
whether to fall back to the agent.
"""
import logging

from . import db, platforms
from .browser_util import first, retry_for_any, visible

log = logging.getLogger("selector_store")


def _candidates(variant_key, role):
    platform = variant_key.split(":", 1)[0]
    learned = db.learned_selectors_for(variant_key, role)
    baseline = platforms.BASELINE_SELECTORS.get(platform, {}).get(role, [])
    # Learned selectors first (most recently successful), then baseline
    # ones not already covered.
    seen = set(learned)
    return learned + [s for s in baseline if s not in seen]


def resolve(page, variant_key, role, timeout_s=3, retry=False, poll_s=3):
    """Return the first matching element for this role, or None. With
    retry=True, keeps re-scanning for timeout_s instead of a single pass --
    use that for fields that may render a few seconds late."""
    selectors = _candidates(variant_key, role)
    if not selectors:
        return None
    if retry:
        return retry_for_any(page, selectors, timeout_s=timeout_s, poll_s=poll_s, log=log)
    return first(page, selectors, timeout=timeout_s * 1000)


def is_visible(page, variant_key, role) -> bool:
    selectors = _candidates(variant_key, role)
    return bool(selectors) and visible(page, selectors)


def record(variant_key, role, selector):
    db.record_selector(variant_key, role, selector)
    log.info("recorded selector for %s/%s: %s", variant_key, role, selector)


def mark_needs_login(variant_key, platform, example_url):
    db.touch_variant(variant_key, platform, example_url, status="needs_login")


def mark_ok(variant_key, platform, example_url):
    db.touch_variant(variant_key, platform, example_url, status="ok")


def is_new_variant(variant_key) -> bool:
    """True if we have no learned selectors and no baseline at all for this
    variant -- i.e. a shape of meeting we've never successfully joined
    before. (A join_variants row may already exist by this point -- run()
    touches it at the very start of every attempt -- so presence of a row
    isn't itself a signal; only accumulated learned selectors are.)"""
    platform = variant_key.split(":", 1)[0]
    if platforms.BASELINE_SELECTORS.get(platform):
        return False
    return not any(db.learned_selectors_for(variant_key, role) for role in platforms.ROLES)
