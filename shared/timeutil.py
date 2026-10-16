# -*- coding: utf-8 -*-
"""Time zones between this device and Chronicle.

Chronicle stores every instant in UTC. Kodi's own ``lastplayed`` is a naive LOCAL clock reading ('YYYY-MM-DD HH:MM:SS',
no zone). The two used to be compared and copied across as if they were the same clock, which left a constant error of
the device's UTC offset (six hours in Mountain time) in every "which side watched it more recently" decision: F9, finished
and rated on one Shield, stayed 'in progress' for weeks on the other (2026-10-11).

So the device now tells Chronicle which zone it is in (header X-Client-Timezone on every request; Chronicle answers
in that zone and reads offset-less timestamps in it), sends every timestamp with its explicit offset, and converts
whatever Chronicle sends (offset-carrying, 'Z', or naive-UTC from an older server) to local time by INSTANT before
comparing or writing it into Kodi. No zone is built in: the zone comes from the device.
"""
import calendar
import os
import re
import time
from datetime import datetime

TIMEZONE_HEADER = 'X-Client-Timezone'

_KODI_FORMAT = '%Y-%m-%d %H:%M:%S'
_ISO_FORMAT = '%Y-%m-%dT%H:%M:%S'
_OFFSET_RE = re.compile(r'(Z|[+-]\d{2}:?\d{2})$')
# Parsed by hand, never with datetime.strptime(): that call has failed unpredictably on Kodi's embedded Python
# (see the 2026-09-13 note in progress_sync.py).
_STAMP_RE = re.compile(r'^\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})')
_IANA_RE = re.compile(r'^[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)+$')


def _format_offset(seconds):
    sign = '+' if seconds >= 0 else '-'
    seconds = abs(int(seconds))
    return '{0}{1:02d}:{2:02d}'.format(sign, seconds // 3600, (seconds % 3600) // 60)


def _offset_at(epoch):
    """UTC offset in seconds of this device's local zone at the given instant (DST aware)."""
    local = time.localtime(epoch)
    return calendar.timegm(local) - int(epoch)


_setting_cache = None


def _kodi_setting_timezone():
    """Kodi's own time zone setting where the platform has one (desktop builds); empty on Android. Asked once per
    process: it is attached to every Chronicle request, and the answer does not change while Kodi runs."""
    global _setting_cache
    if _setting_cache is None:
        try:
            import json
            import xbmc
            reply = json.loads(xbmc.executeJSONRPC(json.dumps({
                'jsonrpc': '2.0', 'id': 1, 'method': 'Settings.GetSettingValue',
                'params': {'setting': 'locale.timezone'}})))
            value = (reply.get('result') or {}).get('value')
            _setting_cache = value if isinstance(value, str) else ''
        except Exception:
            _setting_cache = ''
    return _setting_cache


def device_timezone():
    """What to tell Chronicle: an IANA zone id when the device knows one, otherwise its current UTC offset
    ('-06:00'). Either form lets Chronicle speak this device's local time."""
    for candidate in (os.environ.get('TZ', '').lstrip(':'), _kodi_setting_timezone()):
        if candidate and _IANA_RE.match(candidate):
            return candidate
    try:
        return _format_offset(_offset_at(time.time()))
    except (OverflowError, OSError, ValueError):
        return 'Z'


def parse_naive(text):
    """'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DDTHH:MM:SS' (anything after the seconds ignored) -> naive datetime, or None."""
    match = _STAMP_RE.match(text or '')
    if not match:
        return None
    try:
        return datetime(*[int(g) for g in match.groups()])
    except ValueError:
        return None


def kodi_local_to_iso(kodi_lastplayed):
    """Kodi's naive local 'YYYY-MM-DD HH:MM:SS' -> ISO-8601 with this device's offset AT THAT MOMENT
    ('2026-09-03T19:34:43-06:00'), so Chronicle knows the instant whatever zone it is configured for.
    Unparseable input is returned in ISO shape without an offset (Chronicle then reads it in the declared zone)."""
    if not kodi_lastplayed:
        return None
    local = parse_naive(kodi_lastplayed)
    if local is None:
        return kodi_lastplayed.replace(' ', 'T')
    try:
        epoch = time.mktime(local.timetuple())
        return local.strftime(_ISO_FORMAT) + _format_offset(_offset_at(epoch))
    except (OverflowError, OSError, ValueError):
        return local.strftime(_ISO_FORMAT)


def _iso_to_epoch(iso):
    """Chronicle ISO string -> epoch seconds, or None. A trailing Z / +hh:mm is honoured; a naive value is UTC
    (Chronicle's convention before it learned to answer in the device's zone)."""
    if not iso:
        return None
    iso = iso.strip()
    match = _OFFSET_RE.search(iso)
    offset = 0
    if match:
        token = match.group(1)
        iso = iso[:match.start()]
        if token != 'Z':
            digits = token[1:].replace(':', '')
            offset = (int(digits[:2]) * 3600 + int(digits[2:]) * 60) * (1 if token[0] == '+' else -1)
    naive = parse_naive(iso)
    if naive is None:
        return None
    return calendar.timegm(naive.timetuple()) - offset


def chronicle_iso_to_local(iso):
    """Chronicle ISO string -> naive LOCAL datetime on this device (the same clock Kodi's lastplayed uses), or None."""
    epoch = _iso_to_epoch(iso)
    if epoch is None:
        return None
    try:
        return datetime(*time.localtime(epoch)[:6])
    except (OverflowError, OSError, ValueError):
        return None


def chronicle_iso_to_kodi(iso):
    """Chronicle ISO string -> Kodi's 'YYYY-MM-DD HH:MM:SS' local string, or '' when unparseable."""
    local = chronicle_iso_to_local(iso)
    return local.strftime(_KODI_FORMAT) if local else ''
