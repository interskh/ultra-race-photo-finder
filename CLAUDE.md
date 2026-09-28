# photofinder — agent notes

Personal tool to find the user's own photos in race-photo galleries (faces often covered, night races) by clothing, bib, time, photographer and scene. Local only, Apple M4 16 GB.

## Read first
- `README.md` — usage (download → index → serve).
- `docs/ROADMAP.md` — current status, what's running, ranked next steps, known issues.
- `docs/superpowers/specs/2026-09-27-photo-finder-search-design.md` — architecture, data model, scoring.
- `docs/handoff/2026-09-27-photo-finder-search-design.md` — decisions, model identifiers, measured timings/recall, rejected alternatives. Grep it before re-deciding anything.
- `docs/superpowers/specs/2026-09-28-people-and-originals-design.md` + `docs/handoff/2026-09-28-people-and-originals-design.md` — saved people (profiles, label migration), matched via, originals download; decisions and E2E results.

## Layout
- `src/photofinder/sources/yipai.py` downloader · `index/stages.py` indexing stages · `models.py` model loaders · `search.py` scoring/filters · `evaluate.py` bib-based recall eval · `originals.py` full-size originals job (fresh signed URL by file name, CSV, zip) · `web/app.py` + `web/static/index.html` UI · `cli.py` entrypoint (`photofinder index|search|eval|serve`).
- Data (gitignored) under `data/`: `yipai/<orderId>/{photos/, manifest.sqlite, index.sqlite, download.log, index.log}`, `subsets/` (symlink subsets for experiments: `first2000`, `race925`, `scenemix`), `models/` (all weights/caches), `exports/<collection>/<profile>/{originals/, photos.csv}` (per saved person; `exports/screens/` holds E2E screenshots).

## Rules
- Everything large goes on `/Volumes/Ext1TB` — photos, indexes, weights (`config.setup_model_env()` points HF/torch/ultralytics caches at `data/models`). Never download into `~`.
- uv only. The shell sets `UV_FROZEN=1`; lock-changing commands need `UV_FROZEN=0 uv add ...`.
- Don't hammer yipai360: keep the downloader's pacing (6 workers, page delay, breaker). The downloader fetches only free 1920px previews; originals are fetched only for marked photos by `originals.py` (1 lookup per 6 s — the file-name search is rate-limited at ~10 per 30 s and answers HTTP 500 when exceeded; the job waits 60/120/240 s before giving up; one job per collection).
- Never modify a collection that a running downloader/indexer is writing; experiment on `data/subsets/*`. `index` holds a per-collection lock.
- One heavy model job at a time (16 GB unified memory); the memory guard throttles on system pressure.
- Server models run in a spawned child process (`web.app.ModelWorker`, one `ProcessPoolExecutor(1)`), stopped after `IDLE_UNLOAD` (300 s) without model work; never unload/reload models inside a long-lived process (each cycle keeps ~1 GB, measured). `serve` sets `models.half_precision` (SigLIP2 `pure_fp16` on MPS); indexing stays fp32. `GET /api/models` drives the UI's loading indicator. Tests swap in a thread pool (`test_web.no_real_models`). Details: `docs/handoff/2026-09-28-model-memory.md`.
- Tests: `uv run pytest -q` (257 passing + 1 opt-in real-model test, `PHOTOFINDER_REAL_MODELS=1`, as of 2026-09-29).
