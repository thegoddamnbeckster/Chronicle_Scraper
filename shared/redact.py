# -*- coding: utf-8 -*-
"""Keeps credentials out of anything the add-on shows or logs (URL logins, and the Chronicle API key in JSON).

Kodi source paths can carry a login: smb://user:password@host/share/..., and inside a multipath:// source the same login
is percent-encoded (smb%3a%2f%2fuser%3apassword%40host%2f...). The add-on builds paths from those sources and puts them in
notifications ("Movie collections folder not reachable: ...") and in kodi.log lines, so the username and password were
displayed on screen and written to the log whenever a folder could not be reached (reported 2026-10-13).

redact_credentials() replaces the user and password with "***", keeping everything else (scheme, host, share, path) so the
message is still useful. Logger applies it to every line, and user-facing messages call it explicitly.
"""
import re

# scheme://user:password@host/...  -- the login is everything between "://" and the LAST "@" before the first "/".
_PLAIN = re.compile(r'(?<=://)[^/\s]*@')

# The same inside a percent-encoded multipath: "%3a%2f%2f" is "://", "%40" is "@", "%2f" is "/".
_ENCODED = re.compile(r'(?i)(%3a%2f%2f)((?:(?!%2f)\S)*?)%40')

_MASK = '***'


# An API key in JSON (or in a Python repr of it, where the quotes may be backslash-escaped): "api_key":"chr_live_..."
_API_KEY = re.compile(r"""(?i)((?:\\?["'])api_?key(?:\\?["'])\s*:\s*(?:\\?["']))[^"'\\]*""")


def redact_credentials(text):
    """The text with any login in a URL-style path, and any API key in JSON, replaced by ***. Non-strings and text
    with nothing to hide come back unchanged."""
    if not isinstance(text, str):
        return text
    if 'api' in text.lower():
        text = _API_KEY.sub(lambda m: m.group(1) + _MASK, text)
    if '@' not in text and '%40' not in text.lower():
        return text
    text = _PLAIN.sub(_MASK + '@', text)
    text = _ENCODED.sub(lambda m: m.group(1) + _MASK + '%40', text)
    return text
