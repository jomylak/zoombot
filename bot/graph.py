"""Microsoft Graph: device-code auth, calendar polling, registration-email lookup."""
import logging
import datetime as dt
import msal
import requests
from . import config

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
# Personal Microsoft accounts (outlook.com / hotmail) live under /consumers.
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Calendars.Read", "Mail.Read"]


def _app():
    cache = msal.SerializableTokenCache()
    if config.MSAL_CACHE.exists():
        cache.deserialize(config.MSAL_CACHE.read_text())
    app = msal.PublicClientApplication(
        config.MS_CLIENT_ID, authority=AUTHORITY, token_cache=cache)
    return app, cache


def _save(cache):
    if cache.has_state_changed:
        config.MSAL_CACHE.write_text(cache.serialize())
        config.MSAL_CACHE.chmod(0o600)


def get_token(interactive_ok=False) -> str:
    app, cache = _app()
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result:
        if not interactive_ok:
            raise RuntimeError(
                "No cached Microsoft token. Run: python -m bot.graph login")
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed: {flow}")
        print("\n" + flow["message"] + "\n")
        result = app.acquire_token_by_device_flow(flow)
    _save(cache)
    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description', result)}")
    return result["access_token"]


def _get(path, token, **params):
    r = requests.get(f"{GRAPH}{path}", params=params, timeout=30,
                     headers={"Authorization": f"Bearer {token}",
                              "Prefer": 'outlook.timezone="UTC"'})
    r.raise_for_status()
    return r.json()


def upcoming_events(token, lookahead_minutes: int):
    """Events starting in the next N minutes, tagged with the autojoin category."""
    now = dt.datetime.now(dt.timezone.utc)
    end = now + dt.timedelta(minutes=lookahead_minutes)
    data = _get("/me/calendarView", token,
                startDateTime=now.isoformat(),
                endDateTime=end.isoformat(),
                **{"$select": "id,subject,organizer,start,end,body,location,categories,isCancelled",
                   "$orderby": "start/dateTime",
                   "$top": "50"})
    out = []
    for ev in data.get("value", []):
        if ev.get("isCancelled"):
            continue
        cats = [c.lower() for c in (ev.get("categories") or [])]
        if config.AUTOJOIN_CATEGORY.lower() not in cats:
            continue
        out.append(ev)
    return out


def find_registration_link(token, meeting_id: str):
    """Search mail for the Zoom confirmation carrying a per-registrant tk= link."""
    from .extract import html_to_text, find_zoom_urls, has_registration_token
    if not meeting_id:
        return None
    try:
        data = _get("/me/messages", token,
                    **{"$search": f'"{meeting_id}"', "$top": "10",
                       "$select": "subject,body,receivedDateTime"})
    except requests.HTTPError as e:
        log.warning("mail search failed for %s: %s", meeting_id, e)
        return None
    for msg in data.get("value", []):
        text = html_to_text((msg.get("body") or {}).get("content", ""))
        for url in find_zoom_urls(text):
            if has_registration_token(url):
                log.info("found registration link in mail: %s", msg.get("subject"))
                return url
    return None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        get_token(interactive_ok=True)
        print("Microsoft auth cached at", config.MSAL_CACHE)
    else:
        tok = get_token()
        for ev in upcoming_events(tok, config.LOOKAHEAD_MINUTES):
            print(ev["start"]["dateTime"], "|", ev["subject"], "|", ev.get("categories"))
