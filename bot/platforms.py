"""Platform detection, variant fingerprinting, and Zoom's baseline
selectors (the only hand-written baseline -- everything else is learned
live by the agent, see ai_agent.py / selector_store.py).
"""
import re
from urllib.parse import urlparse, parse_qs

PLATFORM_HOSTS = [
    ("zoom", re.compile(r"(^|\.)zoom\.us$", re.I)),
    ("teams", re.compile(r"(^|\.)teams\.microsoft\.com$|(^|\.)teams\.live\.com$", re.I)),
    ("meet", re.compile(r"(^|\.)meet\.google\.com$", re.I)),
    ("webex", re.compile(r"(^|\.)webex\.com$", re.I)),
]

# Hosts that mean "sign in with a personal account" -- if we land here
# without ever seeing a guest/anonymous join option, abort. This is a
# backstop; the agent is told the same rule directly for platforms/pages
# this list doesn't cover.
IDP_LOGIN_DOMAINS = [
    re.compile(r"(^|\.)accounts\.google\.com$", re.I),
    re.compile(r"(^|\.)login\.microsoftonline\.com$", re.I),
    re.compile(r"(^|\.)login\.live\.com$", re.I),
    re.compile(r"(^|\.)appleid\.apple\.com$", re.I),
    re.compile(r"(^|\.)okta\.com$", re.I),
]


def detect_platform(url: str) -> str:
    host = urlparse(url).hostname or ""
    for name, pattern in PLATFORM_HOSTS:
        if pattern.search(host):
            return name
    return "generic"


def is_idp_login_host(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return any(p.search(host) for p in IDP_LOGIN_DOMAINS)


_HAS_DIGIT = re.compile(r"\d")  # any segment containing a digit is a token, not a route word


def variant_key(url: str) -> str:
    """Fingerprint for 'meetings that look structurally the same', so we
    don't need one learned script per unique meeting URL. Numeric/token
    path segments (meeting IDs, etc.) are collapsed to '*'. Teams in
    particular percent-encodes its meeting-id segment (19%3ameeting_...),
    so this matches on "contains a digit anywhere" rather than requiring
    the whole segment to be plain word characters."""
    parsed = urlparse(url)
    platform = detect_platform(url)
    segments = [seg if not _HAS_DIGIT.search(seg) else "*"
               for seg in parsed.path.strip("/").split("/") if seg]
    path_template = "/".join(segments)
    param_names = ",".join(sorted(parse_qs(parsed.query).keys()))
    return f"{platform}:{parsed.hostname}:{path_template}:{param_names}"


# Roles the join state machine resolves selectors for. Baselines below
# only exist for Zoom -- every other platform starts empty and is learned
# by the agent on first contact (see ai_agent.py).
ROLES = [
    "login_wall", "join_from_browser", "dismiss_media_prompt",
    "name_input", "passcode_input", "join_button",
    "in_meeting", "waiting_room", "meeting_ended", "removed",
    "leave_button", "participant_count",
]

BASELINE_SELECTORS = {
    "zoom": {
        "name_input": ["#input-for-name", "input[placeholder*='name' i]",
                       "#inputname", "input[type='text']"],
        "passcode_input": ["#input-for-pwd", "input[type='password']",
                           "input[aria-label*='passcode' i]",
                           "input[aria-label*='password' i]"],
        "join_button": ["button:has-text('Join')", "#joinBtn",
                        ".preview-join-button", "text=Join"],
        "join_from_browser": ["button:has-text('Join from browser')",
                              ":text('Join from browser')"],
        "dismiss_media_prompt": ["button:has-text('Continue without microphone and camera')",
                                 "button:has-text('Continue without')"],
        "in_meeting": ["#foot-bar", ".footer-participants-button",
                       "[aria-label*='open the participants' i]",
                       ".meeting-info-icon"],
        "waiting_room": [":text('Please wait')", ":text('waiting room')",
                         ":text('host will let you in')",
                         ":text('Host has joined')",
                         ":text('We've let them know')"],
        "leave_button": ["button:has-text('Leave')", ".footer__leave-btn"],
        "meeting_ended": [":text('This meeting has been ended by')",
                          ":text('has ended')", ":text('Meeting ended')"],
        "removed": [":text('You have been removed')",
                    ":text('removed by the host')"],
        "login_wall": ["input[type='password'][name='password']",
                       ":text('Sign In to Join')"],
        "participant_count": [".footer-button__number-counter",
                              "[aria-label*='open the participants' i] span"],
    },
}
