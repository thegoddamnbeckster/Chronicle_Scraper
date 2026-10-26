# Chronicle Scraper

A Kodi metadata scraper for movies and TV shows, backed by your self-hosted [Chronicle](https://github.com/thegoddamnbeckster/Chronicle)
server.

**Addon ID:** `script.chronicle.scraper` (renamed from `metadata.chronicle.python` in v2.12.0)
**Extension points:** `xbmc.metadata.scraper.movies`, `xbmc.metadata.scraper.tvshows`
**Kodi:** 20 "Nexus" and later (uses the modern `InfoTagVideo` Python API throughout)
**Auth:** QR code device authentication (same flow as [Chronicle_Scrobbler](https://github.com/thegoddamnbeckster/Chronicle_Scrobbler))

---

## Not the same thing as Chronicle_Scrobbler

Chronicle already has a Kodi addon: [Chronicle_Scrobbler](https://github.com/thegoddamnbeckster/Chronicle_Scrobbler),
which reports watch progress, ratings, and artwork sync in the background. It's a
service/script addon and — like SIMKL Scrobbler and Trakt's own Kodi addon —
architecturally cannot appear in Kodi's "Change Content" scraper list; scrapers and
scrobblers are different Kodi extension points entirely.

This addon is the other half: real `xbmc.metadata.scraper.movies` and
`xbmc.metadata.scraper.tvshows` addons, selectable from "Change Content" like The
Movie Database or TheTVDB, that supply library metadata during Kodi's own scans.

## What makes it different from a normal scraper

A normal Kodi scraper (TMDB, TVDB, etc.) talks directly to its upstream source. This
one doesn't talk to TMDB, TVDB, or anything else directly at all — it only ever asks
**Chronicle**:

- **Search** (`GET /api/v1/scraper/{movies,tv}/search`) — Chronicle checks its own
  library for a title/year match first. If it's missing, Chronicle resolves-and-creates
  the item itself, through whichever metadata provider plugins you've already configured
  there (the same pipeline every other Chronicle import path uses) — not a fresh,
  independent lookup this addon performs on its own.
- **Details** (`GET /api/v1/scraper/{movies,tv}/details`, plus `tv/episodes` and
  `tv/episode-details` for shows) — everything Chronicle has for the item, mined
  across *every* configured metadata provider's stored data (not just one), then
  handed to Kodi's modern `InfoTagVideo` API:
  - plot, tagline, year, premiered date, MPAA/certification, country, studio/network
  - genres, cast, directors, tags
  - **multiple ratings at once** (e.g. TMDB *and* Trakt *and* TVmaze, if configured),
    not just one picked value
  - **multiple artwork candidates per type** (poster, fanart, banner, clearlogo,
    clearart, discart, characterart) so Kodi's "choose art" screen has real
    alternates, tagged by which provider each one came from
  - trailer (converted to a playable `plugin.video.youtube` link)
  - cross-provider external IDs (IMDB / TVDB / TMDB / Trakt) via `setUniqueIDs`
  - **movie sets** — a movie's Chronicle collection becomes a real Kodi movie set,
    complete with set overview and set artwork
  - **TV seasons and episodes** — every season and episode Chronicle already has
    under a show, with its own art, cast, and ratings, including show title/year,
    air date, and runtime on each episode

The practical effect: Kodi always ends up showing exactly what Chronicle itself would
show for a title. There's no separate "which TMDB result do you want" disambiguation
step in Kodi — Chronicle has already picked one answer via its own confidence-scored
resolution.

Every field above is only ever sent when Chronicle actually has real data for it —
nothing is invented to look more complete than it is.

Every movie or episode Kodi's library scanner successfully resolves through this
scraper also becomes trackable in Chronicle automatically (mirroring how scrobbling
already auto-creates media items on first watch) — scanning your library with this
scraper selected is, at the same time, populating your Chronicle library.

## Local .nfo files are not supported

Chronicle is the only source of what this scraper reports. The addon never reads or
writes `.nfo` files, and Kodi's `NfoUrl` scraper action is not implemented.

Remove any `.nfo` files from folders scanned with this scraper. Kodi reads a local
`.nfo` itself before asking any scraper, so a leftover file can override Chronicle's
data. For TV, an episode-level `.nfo` makes Kodi stop scanning that show at that
episode, because Kodi never falls back to the scraper's normal episode lookup when
`NfoUrl` returns nothing.

## Background service: watch progress in the corner

A background service (`service.py`) shows a corner progress toast (matching Kodi's own
file-scan indicator) while the scrapers are working -- each scraper action
(`find`/`getdetails`/`getepisodedetails`/`getartwork`) is a separate short-lived Kodi
process, so the service polls a small cross-process heartbeat file to know something
is happening. It also runs the periodic watch-history/rating sync and collection art
sync, runs both once after every Kodi library scan, and once per start re-scrapes the items
Chronicle says changed. It never starts a library scan: Kodi pulls from Chronicle when it scans.

## Setup

1. Install this addon (see [Installation](#installation)).
2. Open the addon → enter your Chronicle server's URL (e.g. `http://192.168.1.50:7979`
   or your own domain) → **Connect to Chronicle** → scan the QR code (or enter the
   short code shown) on your phone, sign in, and approve the device.
3. In Kodi, go to **Videos → Files**, right-click a folder → **Change content...** →
   set Content to *Movies* or *TV shows* → set Scraper to **Chronicle Scraper** → OK →
   confirm the library scan.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Chronicle URL | _(empty)_ | Required — your server's host/IP (and port, if not behind a reverse proxy) |
| API Key | _(empty, hidden)_ | Set automatically by the QR device-auth flow |
| Automatically sync watch history and ratings | **On** | Separate, lightweight, non-destructive pass (see "Watch history and ratings sync" below) -- reconciles resume/watched/rating directly via Kodi's own library fields |
| Sync once shortly after Kodi starts | On | |
| Sync every (minutes) | 120 (2h) | 30-minute increments |

## Repository Structure

```
Chronicle_Scraper/
├── addon.xml                      # xbmc.metadata.scraper.{movies,tvshows} + xbmc.python.script
├── default.py                     # Menu: Test Connection / Connect to Chronicle / Settings
├── service.py                     # Background service: corner activity indicator, periodic syncs
├── icon.png                       # Chronicle's own "C" icon
├── LICENSE
├── python/
│   ├── scraper.py                 # Movies: find / getdetails
│   └── tvshow_scraper.py          # TV shows: find / getdetails / getepisodelist / getepisodedetails
├── lib/
│   ├── logger.py                  # xbmc.log wrapper
│   ├── chronicle_client.py        # HTTP client for Chronicle's scraper-facing API
│   ├── kodi_video_info.py         # Shared InfoTagVideo helpers (ratings, artwork, trailer, common fields)
│   ├── kodi_settings.py           # Reads Kodi's own live settings via JSON-RPC (never hardcoded)
│   ├── collection_art_sync.py     # Sets movie-set art on Kodi's sets, only where it differs from Chronicle (per-device change cursor)
│   ├── movie_art_sync.py          # Local poster/fanart sync + streamdetails lookup for movies
│   ├── progress_sync.py           # Shared resume/watched/rating reconciliation logic (used by scrapes AND the periodic sync)
│   ├── watch_rating_sync.py       # Periodic non-destructive watch-history/rating sync (own settings category, own timer)
│   ├── media_id_cache.py          # Persists each item's resolved Chronicle id so watch_rating_sync.py skips re-resolving it
│   ├── settings_upgrade.py        # One-time migration for settings whose shipped default changed after install
│   ├── activity_tracker.py        # Cross-process heartbeat file for the corner activity indicator
│   ├── device_auth.py             # QR device-auth flow (shared design with Chronicle_Scrobbler)
│   └── qr_dialog.py               # QR code + PIN display UI
└── resources/
    ├── settings.xml
    └── language/resource.language.en_gb/strings.po
```

## Movie-set artwork

Kodi's scrapers can offer a set only a poster and a fanart, so the add-on sets the rest
(banner, clearlogo, clearart, discart, thumb) itself, on the set, with
`VideoLibrary.SetMovieSetDetails`, as the image's own web address. Nothing is written to
Kodi's "Movie set information folder" any more, so a share that can't be reached no longer
matters.

It touches Kodi only when something is different:

- **Each Kodi keeps its own copy of the time it last synced.** The add-on sends it to Chronicle
  and asks for the collections changed since (one request); Chronicle keeps nothing per device.
  A library where nothing changed gets an empty answer and nothing is compared, written or
  shown. Several Kodi installs each get exactly what they are missing.
- **Each changed collection is compared with the art Kodi already has**, slot by slot, and
  only the slots that differ are written, so a collection that merely appears in the list
  (one of its movies changed) costs nothing visible.
- **A movie set Kodi has not had before** (new movies in a new collection after a scan, a
  rebuilt library) is not in a "changed since" list, so the add-on keeps the names of the
  sets it has handled and asks for everything when Kodi has a new one. The first run does the
  same, which also moves older sets off their folder files.
- A slot Chronicle has no art for is left alone.

It runs after every library scan, once shortly after Kodi starts, and on the interval in
Settings; **Sync Collection Art Now** compares every collection.

Note: Chronicle's server can *also* write collection art directly into a shared UNC
folder (see the "Kodi Movie Collections" setting in Chronicle itself) — that path is
independent of this addon.

## Known Limitations (v2.13.0)

- **No writers, no movie studios, no sort titles.** Kodi's API supports all three,
  but no metadata provider Chronicle currently has configured populates them for
  movies (writers/studio) — these are simply left unset rather than faked. TV shows
  do get a studio (from network data) since that's genuinely present.
- **Episodes are not auto-created.** `tv/search` resolves-or-creates the *show* the
  same way movies do, but `tv/episodes` only returns episodes Chronicle's own
  file-scanner/import pipeline already knows about. A brand-new show has an empty
  episode list until Chronicle's backend populates it some other way.
- **NfoUrl is not implemented** for either scraper -- local `.nfo` files are not
  supported (see above).
- **Search matches by title/year only**, mirroring Chronicle's own scrobble
  resolve-or-create logic. No fuzzy/alternate-title matching beyond what Chronicle's
  configured metadata providers already do internally.

## Installation

Zip this directory (or clone it directly into Kodi's addons folder as
`script.chronicle.scraper`), then install via Kodi's "Install from zip file" option,
or copy the folder directly into Kodi's `addons/` directory and restart Kodi.

```
<Kodi userdata>/addons/script.chronicle.scraper/
```
