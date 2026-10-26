# -*- coding: utf-8 -*-
"""Kodi's own spelling of an art value, shared by every add-on that compares art against Chronicle's."""
import urllib.parse


def kodi_art_url(value):
    """The plain URL behind an art value as Kodi reports it. Kodi wraps what it stores as
    image://<url-encoded location>/, whether that is a web address or a file -- so comparing a value read from
    VideoLibrary.Get*Details with Chronicle's plain address never matches, and the same art is rewritten on
    every pass (episode thumbnails, 3,000+ per pass, 2026-10-26)."""
    value = value or ''
    if not value.startswith('image://'):
        return value
    return urllib.parse.unquote(value[len('image://'):].rstrip('/'))
