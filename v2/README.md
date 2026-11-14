# cli_debrid 2.0 (work in progress)

A ground-up, much smaller rebuild of cli_debrid. It is one app with its own mount.

- **The brain (Python):** follows your lists, finds releases, picks the best one, and organizes your library.
- **The engine (Go, bundled):** streams debrid and usenet content to a FUSE mount. It's a slimmed copy of cli_mount; see `engine/UPSTREAM.md`.

There's one container, one settings page, and one setup wizard. You never configure the engine by hand.

The old app at the repo root is untouched, and nothing in either app imports from the other.

## What it does
1. **Lists:** your Plex watchlist, Trakt lists and MDBList lists are checked every 10 minutes. You can also add titles by searching.
2. **Metadata:** TMDB looks up each title. TVDB supplies episode numbering for shows, including absolute numbers for anime.
3. **Search:** Torrentio, Zilean, Jackett, Prowlarr and Newznab indexers are searched. When several episodes of a season are wanted together, a season pack is tried first.
4. **Pick:** releases are filtered and ranked using one quality profile (1080p, 4K, or 4K with a 1080p fallback). Cached debrid torrents score higher, and very large files are penalized.
5. **Download:** the release goes to the bundled engine. If a torrent isn't cached or a download fails, the next release is tried.
6. **Library:** a symlink is made with fixed Plex-friendly naming, then Plex scans that folder.
7. **Repair:** when someone uses Plex's "Report issue" on a title, it's replaced. The old file keeps playing until the new one is ready. For 7 days after arrival, a clearly better release is grabbed as an upgrade.

## Item states
- **Wanted**: ready to search.
- **Searching**: being searched now.
- **Downloading**: handed to the engine.
- **Done**: in the library.
- **Waiting**: not released yet, or nothing found. Retried after 1h, then 6h, then daily.
- **Failed**: nothing found after 10 searches. Retried weekly; you can also press "Search now".

## Settings
All settings are on one page. The YAML file (`/data/config.yaml`) also accepts hidden tweaks under `advanced:`, for example:
```yaml
advanced:
  plex_path_map: {"/media": "/data/media"}   # our path -> Plex's path
  episode_delay_hours: 20                     # wait after air date before searching
  engine: {log_level: debug}                  # merged into the engine's config.json
```

## Run (isolated)
```bash
cd v2
CLID2_DATA_DIR=/path/to/data CLID2_MOUNT_DIR=/path/to/mnt CLID2_MEDIA_DIR=/path/to/media \
  docker compose -f docker-compose.v2.yml up -d --build
```
Then open http://host:5060 and the setup wizard starts.

## Develop
```bash
cd v2
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pyflakes
.venv/bin/pytest                      # Python tests
(cd engine && go test ./...)          # engine tests
CLID2_DATA=./data .venv/bin/uvicorn app.main:app --port 5060   # runs without the engine (dev mode)
```

## Live patching
- **Python:** copy the changed file into the container, then restart it.
  ```bash
  docker cp app/acquire.py cli_debrid_v2:/app/app/acquire.py && docker restart cli_debrid_v2
  ```
- **Engine:** build the binary, copy it in, then restart.
  ```bash
  (cd engine && go build -o /tmp/clid2-engine .)   # needs libfuse-dev; build on Debian/Ubuntu for glibc \
    && docker cp /tmp/clid2-engine cli_debrid_v2:/app/bin/clid2-engine && docker restart cli_debrid_v2
  ```

## Layout
| Path | What |
|---|---|
| `app/config.py` | every setting (one YAML file) |
| `app/db.py` | SQLite, single connection + lock |
| `app/worker.py`, `app/pipeline.py` | the one background loop and its fixed cadences |
| `app/catalog.py`, `app/metadata/` | TMDB/TVDB, turning requests into items |
| `app/scrapers/`, `app/ranking/`, `app/search.py` | indexers, parse/filter/score, season-pack planning |
| `app/acquire.py` | pick → download → place, failover, replace/upgrade |
| `app/library/` | file matching, symlinks, Plex scans |
| `app/sources/` | Plex watchlist, Trakt, MDBList |
| `app/repair/` | Plex reported issues, upgrades |
| `app/engine_proc.py`, `app/engine_config.py`, `app/downloads/engine.py` | running and talking to the engine |
| `app/web/` | UI (Jinja2, no JS build step) |
| `engine/` | bundled Go mount engine |

## Not in this version (yet)
- Jellyfin/Emby scans (only Plex scans are built in; symlinks work with any server)
- Plex friends' watchlists, Overseerr/Jellyseerr requests
- Removing items when they leave a list
- `.torrent`-file-only Jackett results (results without a magnet or hash are skipped)
- Multiple quality profiles per title
