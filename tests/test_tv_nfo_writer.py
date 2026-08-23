# -*- coding: utf-8 -*-
"""Tests for lib/tv_nfo_writer.py's <episodeguide> tag.

Kodi prefers a local tvshow.nfo already sitting in the folder over calling
this scraper live -- that's the whole reason this writer exists. But when
Kodi reads the NFO directly instead of invoking get_details(), the only way
it learns how to fetch this show's episode list from Chronicle again (for
new episodes added later, or after Kodi's own library was wiped by a
scraper change and has to rescan from scratch) is the <episodeguide> tag.
Without it, Kodi logs "no episode guide or we are using the local scraper"
for every episode and never calls getepisodelist() at all -- confirmed live
against a real Kodi instance.
"""

import sys
import xml.etree.ElementTree as ET


def _import_tv_nfo_writer():
    sys.path.insert(0, '.')
    import lib.tv_nfo_writer as tv_nfo_writer
    return tv_nfo_writer


def test_build_show_nfo_includes_episodeguide_when_provided(kodi):
    tv_nfo_writer = _import_tv_nfo_writer()
    details = {'title': 'Slow Horses', 'year': 2022}

    root = tv_nfo_writer._build_show_nfo(details, episode_guide='{"chronicleId": 12345}')

    guide = root.find('episodeguide')
    assert guide is not None
    assert guide.text == '{"chronicleId": 12345}'


def test_build_show_nfo_omits_episodeguide_when_not_provided(kodi):
    """No episode_guide given (e.g. a caller that hasn't been updated) must
    not write an empty/broken tag -- omit it entirely rather than write
    something Kodi would parse as a real-but-useless guide."""
    tv_nfo_writer = _import_tv_nfo_writer()
    details = {'title': 'Slow Horses', 'year': 2022}

    root = tv_nfo_writer._build_show_nfo(details)

    assert root.find('episodeguide') is None


def test_build_show_nfo_episodeguide_matches_live_scrape_lookup_string(kodi):
    """The exact bug: the NFO's episodeguide must be byte-for-byte the same
    string tvshow_scraper.py's live get_details() passes to
    vtag.setEpisodeGuide(), so a fresh library scan that reads the NFO
    directly resolves to the same show Chronicle already knows."""
    sys.path.insert(0, 'python')
    import tvshow_scraper
    tv_nfo_writer = _import_tv_nfo_writer()

    show_id = 42
    lookup_string = tvshow_scraper.build_lookup_string(show_id)
    details = {'title': 'Silo', 'year': 2023}

    root = tv_nfo_writer._build_show_nfo(details, episode_guide=lookup_string)

    assert root.find('episodeguide').text == lookup_string
    assert tvshow_scraper.parse_lookup_string(root.find('episodeguide').text) == show_id


def test_show_nfo_xml_is_well_formed_with_episodeguide(kodi):
    """Round-trips the actual serialised XML (not just the in-memory
    ElementTree) through a parser, matching how Kodi itself reads the file."""
    tv_nfo_writer = _import_tv_nfo_writer()
    details = {'title': 'Smoke', 'year': 2025, 'overview': 'A story with "quotes" & <angle brackets>.'}

    root = tv_nfo_writer._build_show_nfo(details, episode_guide='{"chronicleId": 999}')
    xml_bytes = ET.tostring(root, encoding='utf-8')

    reparsed = ET.fromstring(xml_bytes)
    assert reparsed.find('episodeguide').text == '{"chronicleId": 999}'
    assert reparsed.find('plot').text == 'A story with "quotes" & <angle brackets>.'
