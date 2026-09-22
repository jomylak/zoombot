"""Runnable check for the Safe Links / angle-bracket-URL parsing fix.
python test_extract.py
"""
from bot import extract


def test_angle_bracket_url_survives_html_to_text():
    text = extract.html_to_text("Join <https://us02web.zoom.us/j/123456789?pwd=abc123>")
    assert "https://us02web.zoom.us/j/123456789?pwd=abc123" in text, text


def test_safelinks_wrapped_zoom_link():
    body = ("Join Zoom Meeting <https://nam02.safelinks.protection.outlook.com/"
            "?url=https%3A%2F%2Fus02web.zoom.us%2Fj%2F123456789%3Fpwd%3Dabc123&data=x>")
    parsed = extract.parse_event("Sync [AJ]", body, "")
    assert parsed and parsed["platform"] == "zoom"
    assert parsed["meeting_id"] == "123456789"
    assert parsed["passcode"] == "abc123"


def test_safelinks_wrapped_teams_link():
    body = ("Microsoft Teams meeting <https://nam02.safelinks.protection.outlook.com/"
            "?url=https%3A%2F%2Fteams.microsoft.com%2Fl%2Fmeetup-join%2F19%253ameeting_abc"
            "%2540thread.v2%2F0&data=x>")
    parsed = extract.parse_event("Standup [AJ]", body, "")
    assert parsed and parsed["platform"] == "teams"


def test_zoom_events_link_passes_through_unmodified():
    body = "Zoom: https://events.zoom.us/ej/AbCd12-3ef_GhI~jKl4mN5oPq"
    parsed = extract.parse_event("[AJ] Info Session", body, "")
    assert parsed and parsed["platform"] == "zoom"
    assert extract.resolve_join_url(parsed) == parsed["join_url"]


def test_real_html_body_still_stripped():
    text = extract.html_to_text("<p>Hello <b>world</b></p>")
    assert text == "Hello world", text


if __name__ == "__main__":
    test_angle_bracket_url_survives_html_to_text()
    test_safelinks_wrapped_zoom_link()
    test_safelinks_wrapped_teams_link()
    test_zoom_events_link_passes_through_unmodified()
    test_real_html_body_still_stripped()
    print("OK")
