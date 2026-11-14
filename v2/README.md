# cli_debrid 2.0 (work in progress)

A ground-up, much smaller rebuild of cli_debrid. It is one app that bundles its own mount:
- a Python brain finds, picks and organizes media
- a slimmed Go mount engine, copied from cli_mount, streams debrid and usenet content

The old app at the repo root is untouched. Nothing in either app imports from the other.

## Goals
- **About 40 settings on one page.** Hidden tweaks go under `advanced:` in `config.yaml`.
- **One background loop with fixed intervals.** There are no task-timing settings.
- **Six item states:** Wanted → Searching → Downloading → Done, plus Waiting and Failed.
- **No debug or stats pages.**

## Run locally
```bash
cd v2
pip install -r requirements.txt pytest
CLID2_DATA=./data uvicorn app.main:app --port 5060
pytest
```

## Live patching
Copy the changed file into the container, then restart it:
```bash
docker cp app/worker.py cli_debrid_v2:/app/app/worker.py && docker restart cli_debrid_v2
```

## Layout
| Path | What |
|---|---|
| `app/config.py` | every setting (one YAML file) |
| `app/db.py` | SQLite, single connection + lock |
| `app/worker.py` | the background loop |
| `app/pipeline.py` | the steps and their fixed cadences |
| `app/web/` | Jinja2 + htmx UI |
| `engine/` | bundled Go mount engine (phase 4) |
