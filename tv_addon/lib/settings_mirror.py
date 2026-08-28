# -*- coding: utf-8 -*-
"""Offers to mirror a Local-Files setting change into the sibling Chronicle
Scraper addon (Movies <-> TV).

Why this needs to exist at all: script.chronicle.scraper.movie and
script.chronicle.scraper.tv are two entirely separate addon packages, each
with its own settings.xml, ever since the v3.0.0 split (see addon.xml's own
changelog for why they can't be one addon) -- but they both declare an
IDENTICAL "Local Files" category (write_nfo, write_streamdetails) that
controls the exact same kind of local-disk behaviour for the exact same
shared library. A mismatch between the two doesn't break anything visibly --
Kodi's own local-file-wins convention means whichever NFOs either addon
writes, the other happily reads right back -- it just silently means half
the library never gets a local NFO written at all, with nothing on screen
explaining why.

Confirmed live 2026-08-28: TV's write_nfo had drifted to off while the movie
side's stayed on. An in-progress "Rebuild local NFOs" pass (which covers
both movies and TV shows in one combined batch, see lib/nfo_rebuild.py) kept
successfully re-scraping every TV episode for over an hour with its batch-
wait "confirmed" counter frozen the entire time -- every episode refresh
was accepted and processed, but tvshow_scraper.py's own write gate
(`ADDON.getSettingBool('write_nfo') and rebuild_state.is_active()`) silently
skipped the actual file write every single time, since the TV addon reads
its OWN settings.xml, not the movie addon's. Nothing in kodi.log said "TV
NFO writing is off" -- it just looked exactly like a hung batch.
"""

import xbmc
import xbmcaddon
import xbmcgui

from lib.logger import Logger

log = Logger('settings_mirror')

# The two bool settings both packages declare identically (resources/
# settings.xml, "Local Files" category). Deliberately NOT the Connection
# category (chronicle_url/api_key/etc) -- those are per-addon device-auth
# state, each addon runs its own separate Connect flow against its own
# device identity, and blindly copying one into the other would cross wires
# between two otherwise-independent connections rather than fix a real
# mismatch.
MIRRORABLE_SETTINGS = ('write_nfo', 'write_streamdetails')


def snapshot(addon):
    """Reads the current value of every mirrorable setting -- call once
    before whatever might change one (ADDON.openSettings(), an inline
    setSettingBool()), then pass the result to offer_mirror() afterward."""
    return {key: addon.getSettingBool(key) for key in MIRRORABLE_SETTINGS}


def offer_mirror(addon, before, sibling_id):
    """Diffs `before` (an earlier snapshot()) against the current settings;
    for whatever actually changed, and only if the sibling addon is
    installed, offers ONE dialog covering every changed setting to apply the
    identical change there too -- there are only ever two of these settings
    and they sit right next to each other in the same category, so they're
    very likely to change together rather than one at a time.

    sibling_id is the OTHER package's addon id -- this module's own copy in
    lib/ (movie side) is always called with the TV id, and the duplicate
    copy in tv_addon/lib/ is always called with the movie id; see each
    default.py's own call site.
    """
    after = snapshot(addon)
    changed = {key: after[key] for key in MIRRORABLE_SETTINGS if after[key] != before.get(key)}
    if not changed:
        return

    if not xbmc.getCondVisibility('System.HasAddon({0})'.format(sibling_id)):
        log.info('offer_mirror: {0} changed but sibling {1} is not installed -- nothing to mirror to'.format(
                 sorted(changed), sibling_id))
        return

    sibling = xbmcaddon.Addon(sibling_id)
    # The real addon.xml name= attribute, not a localized string -- reading it
    # this way means this dialog never needs its own hardcoded copy of the
    # sibling's display name, which would just be one more thing to keep in
    # sync between the two packages.
    sibling_label = sibling.getAddonInfo('name')

    changed_desc = '\n'.join(
        '{0}: {1}'.format(key, addon.getLocalizedString(32118 if value else 32119))
        for key, value in sorted(changed.items())
    )
    apply_too = xbmcgui.Dialog().yesno(
        addon.getLocalizedString(32000),
        addon.getLocalizedString(32114).format(sibling_label, changed_desc),
        yeslabel=addon.getLocalizedString(32115).format(sibling_label),
        nolabel=addon.getLocalizedString(32116),
    )
    if not apply_too:
        log.info('offer_mirror: user declined to mirror {0} to {1}'.format(sorted(changed), sibling_id))
        return

    for key, value in changed.items():
        sibling.setSettingBool(key, value)
    log.info('offer_mirror: applied {0} to {1}'.format(changed, sibling_id))
    xbmcgui.Dialog().notification(
        addon.getLocalizedString(32000),
        addon.getLocalizedString(32117).format(sibling_label),
    )
