# Chronicle Scraper

A Kodi metadata scraper for movies, backed by your self-hosted [Chronicle](https://github.com/thegoddamnbeckster/Chronicle)
server.

**Addon ID:** `metadata.chronicle.python`
**Extension point:** `xbmc.metadata.scraper.movies`
**Kodi:** 19 "Matrix" and later
**Auth:** QR code device authentication (same flow as [Chronicle_Scrobbler](https://github.com/thegoddamnbeckster/Chronicle_Scrobbler))

---

## Not the same thing as Chronicle_Scrobbler

Chronicle already has a Kodi addon: [Chronicle_Scrobbler](https://github.com/thegoddamnbeckster/Chronicle_Scrobbler),
which reports watch progress, ratings, and artwork sync in the background. It's a
service/script addon and — like SIMKL Scrobbler and Trakt's own Kodi addon —
architecturally cannot appear in Kodi's "Change Content" scraper list; scrapers and
scrobblers are different Kodi extension points entirely.

This addon is the other half: a real `xbmc.metadata.scraper.movies` addon, selectable
from "Change Content" like The Movie Database or TheTVDB, that supplies library
metadata during Kodi's own scans.

## What makes it different from a normal scraper

A normal Kodi scraper (TMDB, TVDB, etc.) talks directly to its upstream source. This
one doesn't talk to TMDB, TVDB, or anything else directly at all — it only ever asks
**Chronicle**:

- **Search** (`GET /api/v1/scraper/movies/search`) — Chronicle checks its own library
  for a title/year match first. If it's missing, Chronicle resolves-and-creates the
  item itself, through whichever metadata provider plugins you've already configured
  there (the same pipeline every other Chronicle import path uses) — not a fresh,
  independent lookup this addon performs on its own.
- **Details** (`GET /api/v1/scraper/movies/details`) — the full resolved metadata
  (plot, cast, genres, rating, artwork) for whatever Chronicle already committed to.

The practical effect: Kodi always ends up showing exactly what Chronicle itself would
show for a title. There's no separate "which TMDB result do you want" disambiguation
step in Kodi — Chronicle has already picked one answer via its own confidence-scored
resolution.

Every movie Kodi's library scanner successfully resolves through this scraper also
becomes trackable in Chronicle automatically (mirroring how scrobbling already
auto-creates media items on first watch) — scanning your library with this scraper
selected is, at the same time, populating your Chronicle library.

## Setup

1. Install this addon (see [Installation](#installation)).
2. Open the addon → enter your Chronicle server's URL (e.g. `http://192.168.1.50:7979`
   or your own domain) → **Connect to Chronicle** → scan the QR code (or enter the
   short code shown) on your phone, sign in, and approve the device.
3. In Kodi, go to **Videos → Files**, right-click a movie folder → **Change content...**
   → set Content to *Movies* → set Scraper to **Chronicle Scraper** → OK → confirm the
   library scan.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Chronicle URL | _(empty)_ | Required — your server's host/IP (and port, if not behind a reverse proxy) |
| API Key | _(empty, hidden)_ | Set automatically by the QR device-auth flow |

## Repository Structure

```
Chronicle_Scraper/
├── addon.xml                      # xbmc.metadata.scraper.movies + xbmc.python.script
├── default.py                     # Menu: Test Connection / Connect to Chronicle / Settings
├── icon.png                       # Chronicle's own "C" icon
├── LICENSE
├── python/
│   └── scraper.py                 # The actual scraper: find / getdetails
├── lib/
│   ├── logger.py                  # xbmc.log wrapper
│   ├── chronicle_client.py        # HTTP client for Chronicle's scraper-facing API
│   ├── device_auth.py             # QR device-auth flow (shared design with Chronicle_Scrobbler)
│   └── qr_dialog.py               # QR code + PIN display UI
└── resources/
    ├── settings.xml
    └── language/resource.language.en_gb/strings.po
```

## Known Limitations (v1.0.0)

- **Movies only.** TV shows are a separate Kodi extension point
  (`xbmc.metadata.scraper.tvshows`) with a more involved contract (show details +
  episode list + episode details as distinct steps) — planned as a follow-up, not
  yet built.
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
