# -*- coding: utf-8 -*-
"""Lets scanning carry on while something is being watched, but at a lower priority.

Kodi's scanner works through the library one item at a time and waits for the scraper's answer for each, so pausing briefly inside
the scraper while a video plays slows the whole scan down (Kodi's own reads of the file shares included) to a trickle that leaves
the stream its bandwidth. Nothing is skipped or stopped: when nothing is playing the pause is zero and the scan runs at full speed.
"""

import xbmc

# Seconds a scraper call waits before it does its work while a video is playing. A scan call is made a few times per item, so this
# stretches each item to a handful of seconds: slow enough to stay out of a stream's way, fast enough that the scan still finishes.
PLAYBACK_PACE_SECONDS = 3.0


def is_video_playing():
    try:
        return bool(xbmc.getCondVisibility('Player.HasVideo'))
    except Exception:  # noqa: BLE001 -- no Kodi to ask (shutdown): behave as if nothing is playing
        return False


def yield_to_playback(wait=None, pace=PLAYBACK_PACE_SECONDS):
    """Waits `pace` seconds when a video is playing; returns True when it waited. `wait(seconds)` is injectable for tests; by default it
    is Kodi's abort-aware wait, so a Kodi shutdown is never held up by it."""
    if not is_video_playing():
        return False
    (wait or xbmc.Monitor().waitForAbort)(pace)
    return True
