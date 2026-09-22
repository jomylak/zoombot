"""Pull meeting join details out of calendar-invite / email bodies.
Zoom gets full meeting-id/passcode extraction (needed to build the
app.zoom.us/wc/ web-client URL); every other platform's join link already
works as-is when clicked, so it's just located and tagged with a platform.
"""
import re
from urllib.parse import urlparse, parse_qs, unquote
from bs4 import BeautifulSoup

from . import platforms

ZOOM_URL_RE = re.compile(
    # ej/e = Zoom Events (events.zoom.us/ej/<opaque-token>, no numeric
    # meeting id) -- a registration/info-session link, already fully
    # joinable as-is same as the j/w/my meeting links.
    r"https?://(?:[\w.-]+\.)?zoom\.us/(?:j|w|wc/join|my|ej|e)/[^\s\"'<>\)\]]+", re.I)
# Separator must not swallow letters -- `\D` would turn "Passcode: hunter2"
# into "ter2" by eating "hun" as part of the gap.
_GAP = r"[\s:=\-\u00a0]{0,12}"
MEETING_ID_RE = re.compile(rf"(?:meeting|webinar)\s*id{_GAP}((?:\d[\s\-]?){{9,13}})", re.I)
PASSCODE_RE = re.compile(rf"(?:passcode|password){_GAP}([A-Za-z0-9!@#$%^&*_+=-]{{4,32}})", re.I)

# Other platforms' join links work unmodified -- just need to be found and
# tagged. Ordered narrowest-first; the generic fallback below catches
# anything else that looks meeting-shaped.
OTHER_PLATFORM_URL_RES = [
    ("teams", re.compile(
        r"https?://(?:[\w.-]+\.)?teams\.(?:microsoft|live)\.com/[^\s\"'<>\)\]]+", re.I)),
    ("meet", re.compile(
        r"https?://meet\.google\.com/[a-z\-]+(?:\?[^\s\"'<>\)\]]*)?", re.I)),
    ("webex", re.compile(
        r"https?://(?:[\w.-]+\.)?webex\.com/(?:meet|join)[^\s\"'<>\)\]]*", re.I)),
]
# Last resort for platforms we don't recognize by hostname: a URL sitting
# right next to meeting-ish wording in the invite.
GENERIC_JOIN_URL_RE = re.compile(
    r"(?:join(?:ing)?\s+the\s+meeting|click\s+here\s+to\s+join|join\s+meeting)"
    r"[^\n]{0,80}?(https?://[^\s\"'<>\)\]]+)", re.I)

# Exchange/Outlook Safe Links (ATP) rewrites every URL in a scanned invite to
# https://*.safelinks.protection.outlook.com/?url=<encoded-real-url>&data=...
# -- the real zoom.us/teams.microsoft.com hostname is gone, so none of the
# platform regexes above ever match. Unwrap it back to the real URL first.
SAFELINKS_RE = re.compile(
    r"https?://[\w.-]*safelinks\.protection\.outlook\.com/\?url=([^&\s\"'<>]+)[^\s\"'<>]*", re.I)


def _unwrap_safelinks(text: str) -> str:
    return SAFELINKS_RE.sub(lambda m: unquote(m.group(1)), text or "")


RFC5322_ANGLE_URL_RE = re.compile(r"<(https?://[^\s<>]+)>", re.I)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    # Plain-text calendar bodies (the common case) still wrap bare URLs in
    # angle brackets per RFC 5322, e.g. "<https://zoom.us/j/123>". Unwrap
    # those first -- otherwise the "<" below misdetects the whole body as
    # HTML and BeautifulSoup silently eats the bracketed URL as a bogus tag.
    html = RFC5322_ANGLE_URL_RE.sub(r"\1", html)
    if "<" not in html:
        return html
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def find_zoom_urls(text: str):
    """All Zoom URLs in a blob, de-duped, order preserved."""
    seen, out = set(), []
    for m in ZOOM_URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def meeting_id_from_url(url: str):
    m = re.search(r"/(?:j|w|wc/join|my)/(\d{9,13})", url)
    return m.group(1) if m else None


def pwd_from_url(url: str):
    q = parse_qs(urlparse(url).query)
    return (q.get("pwd") or [None])[0]


def has_registration_token(url: str) -> bool:
    """Zoom registration links carry a per-registrant `tk` token."""
    return "tk=" in url


def find_other_platform_url(text: str):
    """First non-Zoom join URL found, tagged with its platform. Tries known
    hosts first, then falls back to a URL sitting next to "join the
    meeting"-style wording so unrecognized platforms still get *something*."""
    for platform, pattern in OTHER_PLATFORM_URL_RES:
        m = pattern.search(text or "")
        if m:
            return platform, m.group(0).rstrip(".,;")
    m = GENERIC_JOIN_URL_RE.search(text or "")
    if m:
        url = m.group(1).rstrip(".,;")
        return platforms.detect_platform(url), url
    return None, None


def parse_event(subject: str, body_html: str, location: str):
    """Returns dict with join_url / meeting_id / passcode / platform, or None."""
    text = " ".join(filter(None, [html_to_text(body_html), location or "", subject or ""]))
    text = _unwrap_safelinks(text)
    urls = find_zoom_urls(text)
    if urls:
        # Prefer a link that already carries a registration token.
        url = next((u for u in urls if has_registration_token(u)), urls[0])
        return {"join_url": url,
                "meeting_id": meeting_id_from_url(url),
                "passcode": pwd_from_url(url),
                "platform": "zoom"}

    platform, url = find_other_platform_url(text)
    if url:
        return {"join_url": url, "meeting_id": None, "passcode": None,
                "platform": platform}

    # Some Zoom invites give ID + passcode without a clickable link.
    mid = MEETING_ID_RE.search(text)
    if not mid:
        return None
    meeting_id = re.sub(r"\D", "", mid.group(1))
    pw = PASSCODE_RE.search(text)
    return {"join_url": None, "meeting_id": meeting_id,
            "passcode": pw.group(1) if pw else None, "platform": "zoom"}


def web_client_url(meeting_id: str, passcode: str = None, raw_url: str = None) -> str:
    """Build a browser-joinable Zoom URL.

    If we already have a registration link (tk=) or there's no numeric
    meeting_id to build from (e.g. a Zoom Events /ej/<opaque-token> link),
    hand the original URL back untouched -- it's already fully joinable,
    and rewriting it would drop the registrant identity the host reports on.
    """
    if raw_url and (not meeting_id or has_registration_token(raw_url)):
        return raw_url
    base = f"https://app.zoom.us/wc/{meeting_id}/join?fromPWA=1"
    if passcode:
        base += f"&pwd={passcode}"
    return base


def resolve_join_url(parsed: dict) -> str:
    """Platform-aware version of the above: Zoom needs its web-client URL
    built from meeting_id/passcode; every other platform's join_url already
    works as found in the invite."""
    if parsed.get("platform") == "zoom":
        return web_client_url(parsed["meeting_id"], parsed["passcode"], parsed["join_url"])
    return parsed["join_url"]
