# Chronicle Scraper

A Kodi metadata scraper for movies and TV shows, backed by your self-hosted [Chronicle](https://github.com/thegoddamnbeckster/Chronicle)
server.

**Addon ID:** `metadata.chronicle.python`
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
    under a show, with its own art, cast, and ratings

The practical effect: Kodi always ends up showing exactly what Chronicle itself would
show for a title. There's no separate "which TMDB result do you want" disambiguation
step in Kodi — Chronicle has already picked one answer via its own confidence-scored
resolution.

Every field above is only ever sent when Chronicle actually has real data for it —
nothing is invented to look more complete than it is. Chronicle currently has **no**
provider-backed data for writers, movie studios (as opposed to TV networks), or sort
titles, so this addon simply doesn't set those, even though Kodi's API supports them.

Every movie or episode Kodi's library scanner successfully resolves through this
scraper also becomes trackable in Chronicle automatically (mirroring how scrobbling
already auto-creates media items on first watch) — scanning your library with this
scraper selected is, at the same time, populating your Chronicle library.

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

## Repository Structure

```
Chronicle_Scraper/
├── addon.xml                      # xbmc.metadata.scraper.{movies,tvshows} + xbmc.python.script
├── default.py                     # Menu: Test Connection / Connect to Chronicle / Settings
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
│   ├── collection_sync.py         # Fills missing set poster/fanart in Kodi's local movie-sets folder
│   ├── device_auth.py             # QR device-auth flow (shared design with Chronicle_Scrobbler)
│   └── qr_dialog.py               # QR code + PIN display UI
└── resources/
    ├── settings.xml
    └── language/resource.language.en_gb/strings.po
```

## Local movie-set artwork

Kodi checks its own configured **Movie set information folder**
(Settings → Media → Library → "Movie set information folder", exposed as
`videolibrary.moviesetsfolder`) for a set's poster/fanart *before* it ever
looks at anything a scraper offers, and uses a local file there if one
exists — even a broken reference to a file that no longer exists, which just
renders as a blank card instead of falling back to the scraper's art.

This addon reads that setting live via Kodi's own Settings API on every
scrape (it's different per install, so it's never hardcoded) and, when a
movie belongs to a collection:

- If the setting isn't configured at all, it does nothing — that's a
  deliberate choice, not a problem to flag.
- If the set's folder or poster/fanart is **missing**, it downloads
  Chronicle's current picks and writes them there, so the set stops showing
  a blank/broken card.
- If a poster or fanart file **already exists** there, it's left alone —
  this never overwrites artwork you (or a previous scraper) already placed.
- If the folder can't be created or written to (unreachable share, read-only
  mount), Kodi shows a notification naming the configured base path (not any
  particular movie or set — the folder itself is the problem). A whole
  library scan can touch dozens of sets in seconds, so this is throttled to
  at most one popup roughly every 10 minutes rather than once per movie.

## Known Limitations (v2.1.0)

- **No writers, no movie studios, no sort titles.** Kodi's API supports all three,
  but no metadata provider Chronicle currently has configured populates them for
  movies (writers/studio) — these are simply left unset rather than faked. TV shows
  do get a studio (from network data) since that's genuinely present.
- **Episodes are not auto-created.** `tv/search` resolves-or-creates the *show* the
  same way movies do, but `tv/episodes` only returns episodes Chronicle's own
  file-scanner/import pipeline already knows about. A brand-new show has an empty
  episode list until Chronicle's backend populates it some other way.
- **NfoUrl and getartwork actions are not implemented** for either scraper, matching
  v1.0.0's own precedent — Kodi will fall back to its normal find/getdetails flow.
- **Search matches by title/year only**, mirroring Chronicle's own scrobble
  resolve-or-create logic. No fuzzy/alternate-title matching beyond what Chronicle's
  configured metadata providers already do internally.

## Installation

Zip this directory (or clone it directly into Kodi's addons folder as
`metadata.chronicle.python`), then install via Kodi's "Install from zip file" option,
or copy the folder directly into Kodi's `addons/` directory and restart Kodi.

```
<Kodi userdata>/addons/metadata.chronicle.python/
```
