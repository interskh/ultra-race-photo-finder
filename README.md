# photofinder

Find your own photos among thousands of race photos — by clothing, bib number, time, photographer, or scene ("雪山", "finish arch"). Local only; runs on an Apple Silicon Mac. All photos, indexes and model weights live on `/Volumes/Ext1TB`.

Design: `docs/superpowers/specs/2026-09-27-photo-finder-search-design.md` · build log and measured results: `docs/handoff/2026-09-27-photo-finder-search-design.md`.

## 1. Download a yipai360 gallery

```
scripts/download_yipai.sh <orderId>        # orderId is in the gallery URL
tail -f data/yipai/<orderId>/download.log
```

Runs detached under `caffeinate`, paced, resumable. Rerun the same command a day or two later to pick up photos uploaded after the race. Only the free 1920px previews are downloaded; buy originals on the site.

## 2. Index

```
uv run photofinder index data/yipai/<orderId>
```

Stages: scan (EXIF time, photographer, album) → detect people → clothing embeddings → scene embeddings. Add `--ocr` to also read bib numbers with Apple Vision (optional and slow: ~2–3 h for 190k people). Resumable and incremental; throttles under memory pressure and stops (resumable) if its own footprint passes 6 GB. Any folder of JPEGs works as a collection.

## 3. Search in the browser

```
uv run photofinder serve data/yipai/<orderId>     # http://127.0.0.1:8000/
```

What works best (measured in the handoff log): clothing alone is weak when many runners wear the same event jacket, so iterate:

1. Upload a photo of you (race day, same kit) and click your box — or start from your bib number if the index was built with `--ocr`.
2. Mark results **Me** / **Not me**.
3. **Find more like my marked ones** — searches with all your marked shots, which is how other photographers' photos of you surface.
4. Narrow with time, photographer, album; add a scene or outfit description.
5. **My photos → Export** writes `data/exports/<collection>/<profile>/photos.csv` (UTF-8 with BOM, opens in Excel): source photo id, original file name (searchable on the site), photographer, time, album, preview/original paths and download status. For yipai360 galleries the API (`POST /api/originals`) downloads the full-size originals into `.../<profile>/originals/` (paced ≤1 lookup/s, resumable); photos refused by the site are listed as `buy on site: <reason>`.

## CLI search / evaluation

```
uv run photofinder search <collection> --photo me.jpg [--box N] [--scene 雪山] [--bib 8038] [--from ... --to ...]
uv run photofinder eval <collection> --bib 8038      # recall of clothing search against OCR'd bib ground truth
```

## Tests

```
uv run pytest -q
```
