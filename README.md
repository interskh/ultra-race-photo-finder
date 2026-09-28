# photofinder

Find your own photos among thousands of race photos — by clothing, bib number, time, photographer, or scene ("雪山", "finish arch"). Local only; runs on an Apple Silicon Mac. All photos, indexes and model weights live on `/Volumes/Ext1TB`.

Design: `docs/superpowers/specs/2026-09-27-photo-finder-search-design.md` · build log and measured results: `docs/handoff/2026-09-27-photo-finder-search-design.md`.

## 1. Download a yipai360 gallery

```
scripts/download_yipai.sh <orderId>        # orderId is in the gallery URL
tail -f data/yipai/<orderId>/download.log
```

Runs detached under `caffeinate`, paced, resumable. Rerun the same command a day or two later to pick up photos uploaded after the race. The downloader fetches only the free 1920px previews; full-size originals of the photos you mark come later from the web UI (step 3).

## 2. Index

```
uv run photofinder index data/yipai/<orderId>
```

Stages: scan (EXIF time, photographer, album) → detect people → clothing embeddings → scene embeddings. Add `--ocr` to also read bib numbers with Apple Vision (optional and slow: ~2–3 h for 190k people). Resumable and incremental; throttles under memory pressure and stops (resumable) if its own footprint passes 6 GB. Any folder of JPEGs works as a collection.

## 3. Search in the browser

```
uv run photofinder serve data/yipai/<orderId>     # http://127.0.0.1:8000/
```

The server loads models only for uploads and description/scene searches (SigLIP2 in half precision on the GPU), and unloads them after 5 minutes without such a request, so an idle server drops from about 2.9 GB back to about 1.5 GB (the rest is the embedding index). The next upload or description search reloads them (15–20 s); the busy bar says "Loading the … model" meanwhile. Half precision halves the weights (1.4 → 0.7 GB) but saves only about 0.25 GB of process memory, because the GPU allocator keeps extra heap.

What works best (measured in the handoff log): clothing alone is weak when many runners wear the same event jacket, so iterate:

1. Upload a photo of you (race day, same kit) and click your box — or start from your bib number if the index was built with `--ocr`.
2. Mark results **✓ <name>** / **✗ Not <name>** (the active person, "Me" by default).
3. **Find more like my marked ones** — searches with all your marked shots, which is how other photographers' photos of you surface. When the search uses two or more marked people, each result shows a small **matched via** thumbnail: the marked photo it resembled most; click it to jump to that photo in My photos. Changed clothes (jacket on/off)? Mark one photo of each look.
4. Narrow with time, photographer, album; add a scene or outfit description.
5. **My photos** lists the marked photos with each one's original status (✓ original / `buy on site: <reason>` / `failed: …`):
   - **Download originals** (yipai360 galleries only) fetches the full-size originals into `data/exports/<collection>/<person>/originals/<YYYYMMDD-HHMMSS>_<photographer>_<source photo id>.jpg` — one lookup per second, skips files already there, shows `n / N`, the current file and errors, and can be cancelled; rerun to resume. Photos the site refuses are listed as `buy on site: <reason>`.
   - **Download as zip** streams that person's originals folder plus `photos.csv` to the browser (e.g. to move them to a phone).
   - **Export CSV** writes `data/exports/<collection>/<person>/photos.csv` (UTF-8 with BOM, opens in Excel): source photo id, original file name (searchable on the site), photographer, time, album, preview/original paths and download status.
   - In the photo viewer, **Download original** fetches one photo, saves it into the same folder and hands it to the browser.

   Originals are exactly what the site's own 下载 button gives: full resolution with EXIF, but for FUGA galleries with the organizer's branding band along the bottom (the signed URL applies it). An unbranded source was not probed.

**Several people.** `Searching for: [Me ▾]` in the top bar switches between saved people; **+ New person** adds one (e.g. a friend), **Rename** / **Delete** act on the active one (delete removes only that person's marks; the last person can't be deleted). Each person has their own marks, Find more, My photos, CSV and originals folder. The browser remembers the active person.

**Keyboard.** On a focused result card (Tab to it) or in the photo viewer: `M` = this is <name>, `N` = not <name> (press again to clear), `←` / `→` previous / next, `Esc` closes the viewer. Shortcuts are off while typing in a text box.

**Upgrading an existing index.** The first time this version opens an index made by an older version it moves the old Me / Not me marks into the person "Me" (one-way). A server still running the older code on that index can no longer save marks — restart it on the new code.

## CLI search / evaluation

```
uv run photofinder search <collection> --photo me.jpg [--box N] [--scene 雪山] [--bib 8038] [--from ... --to ...]
uv run photofinder eval <collection> --bib 8038      # recall of clothing search against OCR'd bib ground truth
```

## Tests

```
uv run pytest -q
```
