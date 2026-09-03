"""Detects an on-screen attendance/QR-code prompt during a meeting.

Checked from the in-meeting monitor loop in joiner.py. Cheap on a miss (one
screenshot, one QR decode); OCR only runs when the QR decode finds nothing.
"""
import io
import logging

from PIL import Image
from pyzbar.pyzbar import decode as zbar_decode
import pytesseract

from . import config

log = logging.getLogger(__name__)


def _keywords():
    return [k.strip().lower() for k in config.ATTENDANCE_KEYWORDS.split(",") if k.strip()]


def check(page):
    """Screenshot the page and look for a QR code or an attendance-related
    keyword. Returns (matched: bool, reason: str | None, image: PIL.Image | None).
    Never raises -- a failed check must not take down the meeting loop."""
    try:
        png_bytes = page.screenshot()
        image = Image.open(io.BytesIO(png_bytes))
    except Exception as e:
        log.warning("attendance screenshot failed: %s", e)
        return False, None, None

    try:
        if zbar_decode(image):
            return True, "qr_code", image
    except Exception as e:
        log.warning("qr decode failed: %s", e)

    try:
        text = pytesseract.image_to_string(image).lower()
        for kw in _keywords():
            if kw in text:
                return True, f"text:{kw}", image
    except Exception as e:
        log.warning("ocr check failed: %s", e)

    return False, None, None
