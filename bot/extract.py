"""Pull Zoom join details out of calendar-invite / email bodies."""
import re
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup

ZOOM_URL_RE = re.compile(
    r"https?://(?:[\w.-]+\.)?zoom\.us/(?:j|w|wc/join|my)/[^\s\"'<>\)\]]+", re.I)
# Separator must not swallow letters -- `\D` would turn "Passcode: hunter2"
# into "ter2" by eating "hun" as part of the gap.
_GAP = r"[\s:=\-\u00a0]{0,12}"
MEETING_ID_RE = re.compile(rf"(?:meeting|webinar)\s*id{_GAP}((?:\d[\s\-]?){{9,13}})", re.I)
PASSCODE_RE = re.compile(rf"(?:passcode|password){_GAP}([A-Za-z0-9!@#$%^&*_+=-]{{4,32}})", re.I)


def html_to_text(html: str) -> str:
    if not html:
        return ""
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


def parse_event(subject: str, body_html: str, location: str):
    """Returns dict with join_url / meeting_id / passcode, or None."""
    text = " ".join(filter(None, [html_to_text(body_html), location or "", subject or ""]))
    urls = find_zoom_urls(text)
    if not urls:
        # Some invites give ID + passcode without a clickable link.
        mid = MEETING_ID_RE.search(text)
        if not mid:
            return None
        meeting_id = re.sub(r"\D", "", mid.group(1))
        pw = PASSCODE_RE.search(text)
        return {"join_url": None, "meeting_id": meeting_id,
                "passcode": pw.group(1) if pw else None}

    # Prefer a link that already carries a registration token.
    url = next((u for u in urls if has_registration_token(u)), urls[0])
    return {"join_url": url,
            "meeting_id": meeting_id_from_url(url),
            "passcode": pwd_from_url(url)}


def web_client_url(meeting_id: str, passcode: str = None, raw_url: str = None) -> str:
    """Build a browser-joinable URL.

    If we already have a registration link (tk=), hand it back untouched --
    rewriting it would drop the registrant identity the host reports on.
    """
    if raw_url and has_registration_token(raw_url):
        return raw_url
    base = f"https://app.zoom.us/wc/{meeting_id}/join?fromPWA=1"
    if passcode:
        base += f"&pwd={passcode}"
    return base
