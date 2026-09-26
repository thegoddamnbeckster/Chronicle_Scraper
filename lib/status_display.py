# -*- coding: utf-8 -*-
"""What the corner status says, and when it is shown.

Per-user requirement (2026-09-26): there must never be a stretch where the library is busy and
nothing on screen says so -- Kodi refuses "Clean library" while a scan or the scraper tail is
running, and a silent refusal reads as "something is broken". So the status is shown whenever ANY
of these is true, and never hidden just because Kodi's own (often invisible, e.g. when a scan is
launched without dialogs) indicator might be up.
"""


def status_text(kodi_scanning, cleaning, scraper_active, scraper_message, scanning_text, cleaning_text):
    """The corner status line, or None when nothing is happening."""
    if cleaning:
        return cleaning_text
    if scraper_active:
        return scraper_message
    if kodi_scanning:
        return scanning_text
    return None
