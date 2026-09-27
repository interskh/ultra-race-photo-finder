# Photo Finder — search & indexing design

Date: 2026-09-27 · Status: approved in chat ("sounds good, let's start building") · Review status: design approved conversationally; no separate spec-review pass.

## Goal

Find my own photos among tens of thousands of race photos when faces are often covered or it is dark. Search by **clothing/appearance** (reference photo of me), **time**, **photographer/album**, **scene text** ("mountain background"), and **bib number**. Personal, local, runs on an M4 Mac with 16 GB unified memory.

## Decisions already made

- **Source-agnostic**: search works on any folder of photos. The yipai360 downloader (`photofinder.sources.yipai`, done) is the first source and writes `data/yipai/<orderId>/{photos/<photoId>.jpg, manifest.sqlite}`; manifest adds photographer (`uid` → nickname) and album (`tag_id` → name).
- **All data on the external disk** (`/Volumes/Ext1TB/...`), including model weights and caches. Fail loudly if the disk is not mounted.
- **Small models, one at a time**, sequential stages, adaptive batch size driven by macOS memory pressure.
- **Local only**: CLI + a localhost web page. No accounts, no cloud.
- Test identity: user's bib is unknown to us; use a **random bib (e.g. 8038)** found via OCR as a stand-in "me" for evaluation.

## Architecture

```
photos folder (+ optional manifest.sqlite)
      │  photofinder index <collection>
      ▼
 scan ─► detect ─► embed_persons ─► embed_scenes ─► ocr_bibs      (each stage resumable, incremental)
      │
      ▼
 <collection>/index.sqlite   (photos, persons, embeddings as float16 BLOBs, labels)
      │
      ├─► photofinder search / eval   (CLI)
      └─► photofinder serve           (FastAPI + static page on localhost)
```

A **collection** is a directory. Images are found recursively (`.jpg/.jpeg/.png`, skipping `*.part` and the index itself). If `<collection>/manifest.sqlite` exists (yipai), photographer/album are joined by `photo_id` = file stem. Index lives at `<collection>/index.sqlite`.

### Modules (`src/photofinder/`)

| Module | Responsibility | Depends on |
|---|---|---|
| `config.py` | Data root, model cache dir (`/Volumes/Ext1TB/Projects/photo-finder/data/models`; sets `HF_HOME`, `TORCH_HOME`, ultralytics weights dir before any model import), mount check, device (`mps` → `cpu` fallback) | — |
| `memory.py` | `pressure_level()` via `sysctl kern.memorystatus_vm_pressure_level` (1 normal / 2 warn / 4 critical); `AdaptiveBatcher`: halve batch on warn (min 1), pause+poll on critical, grow back ×2 after 20 normal batches up to max; periodic log of level, batch size, process RSS | — |
| `db.py` | Schema + connection helpers for `index.sqlite` | — |
| `models.py` | Lazy loaders + `unload()` for: person detector, OSNet re-ID, SigLIP2 (image + text towers), OCR | config |
| `index/stages.py` | `scan`, `detect`, `embed_persons`, `embed_scenes`, `ocr_bibs`; each processes only rows not yet done, commits per batch | db, models, memory |
| `search.py` | Pure numpy scoring over loaded embeddings; filters; group persons → photos | db |
| `cli.py` | `photofinder index|search|eval|serve` (argparse) | all |
| `web/app.py`, `web/static/index.html` | FastAPI API + one static page, no build step | search, models |

### Models (all local, weights cached on Ext1TB)

| Purpose | Model | Notes |
|---|---|---|
| Person detection | Ultralytics **YOLO26s** (`yolo26s.pt`; fall back to `yolo11s.pt` if unavailable) | class 0 only, conf ≥ 0.35, `imgsz=1280` (many photos are 1920-wide panoramas with small runners); keep boxes with height ≥ 96 px |
| Clothing re-ID | **OSNet x1_0 (MSMT17 weights)** via boxmot or torchreid | 256×128 crops → 512-d |
| Crop + scene semantics, text queries | **SigLIP2 ViT-B/16** via open_clip (multilingual tokenizer → Chinese queries work) | 768-d-ish; same model for person crops, whole images, and text |
| Bib OCR | **Apple Vision** via `ocrmac` | runs on person crop; keep digit tokens of length 3–5 with confidence |

Exact package/model identifiers are verified against current docs at implementation time; the doer records the chosen identifiers in the handoff log.

### Data model (`index.sqlite`)

- `photos(id pk, relpath unique, source_photo_id, width, height, taken_at text 'YYYY-MM-DD HH:MM:SS' camera-local, taken_ts real, camera, photographer_uid, photographer, album, scanned_at, detected_at, scene_done_at, status, error)`
- `persons(id pk, photo_id fk, x1, y1, x2, y2, conf, embedded_at, ocr_at)`
- `emb_person_osnet(person_id pk, v blob)`, `emb_person_siglip(person_id pk, v blob)`, `emb_scene_siglip(photo_id pk, v blob)` — L2-normalized float16.
- `bibs(person_id, text, conf)` — zero or more candidates per person.
- `labels(person_id pk, label 'me'|'not_me', created_at)` — persists refinement across sessions.

### Search scoring

Inputs: reference person vectors (from indexed persons or an uploaded photo's selected box), optional person text ("orange vest black shorts"), optional scene text ("mountain"), filters (time range on `taken_at`, photographers, albums, bib substring), `me`/`not_me` labels.

Per person: `score = w_reid·max cos(osnet, refs) + w_clip·max cos(siglip_crop, refs) + w_text·cos(siglip_crop, text) + w_scene·cos(scene(photo), scene_text) + bib_bonus − w_neg·max cos(osnet, not_me)`. Terms with no input are dropped and weights renormalized. `me` labels are added to refs. Defaults `w_reid = w_clip = 0.5`, `bib_bonus = 0.3`, `w_neg = 0.3`; tuned via `eval`. Photos are ranked by their best person; results carry the matched box. Brute-force float16 matmul in chunks — target < 2 s over ~350k persons.

### Time

`taken_at` is camera-local EXIF `DateTimeOriginal` (fallback: file mtime, flagged). Photographer clocks differ, so the UI shows time filtering together with a photographer filter; no clock-skew correction in v1.

### Error handling (normal-user failure paths)

- Disk not mounted / collection missing → one-line error, non-zero exit.
- Corrupt/unreadable image → `photos.status='error'` with message, stage continues.
- Photo without EXIF → `taken_at` NULL, still searchable, excluded only when a time filter is set.
- Index interrupted → rerun resumes; nothing recomputed for completed rows.
- Downloader still writing → only complete `*.jpg` files are seen (downloader renames atomically); rerun `index` picks up new photos.
- Query photo with no detected person → API/CLI says so; user may draw/choose whole image as fallback (CLI: `--whole`).
- Search before any embeddings exist → clear message naming the missing stage.
- Memory pressure critical → stage pauses and logs; resumes when pressure drops.

## Acceptance checklist

1. `photofinder index <collection>` on a real subset completes; every image gets a `photos` row; rerun does no model work for completed rows (asserted via counts/logs).
2. Memory guard: with an injected pressure reader, warn halves batch size, critical pauses; RSS and level are logged.
3. `photofinder search <collection> --photo ref.jpg [--box N]` prints ranked photos and writes a contact-sheet JPEG of top-k crops.
4. Scene text query "mountain" ranks mountain photos above finish-arch/night-stage photos on a real subset (visual check, recorded in handoff).
5. Filters (time window, photographer, album, bib) restrict results correctly (unit tests on a fixture index).
6. `photofinder eval <collection> --bib 8038`: finds bib-8038 persons by OCR, uses one as reference with bib signal disabled, reports recall@10/@50 of the other bib-8038 photos, and writes a contact sheet; if the bib is not found, lists the most frequent bibs instead.
7. `photofinder serve <collection>` → page: upload a photo, click a box, see ranked results with filters and scene text; mark me/not-me (persisted); export selected to `data/exports/<collection-name>-<date>.txt` (photo ids + paths). Verified with a real browser screenshot.
8. Full `pytest` passes.

## Slice plan

### Slice 1 — index + search-by-photo from the CLI (end-to-end proof)
Depends on: none (downloaded photos exist).
Tasks:
1. `config`, `memory` (AdaptiveBatcher + tests), `db` schema, `scan` stage (EXIF time/camera, manifest join, error rows), CLI `index` skeleton.
2. `models` loaders for YOLO + OSNet + SigLIP2 image tower; `detect` and `embed_persons` stages with resume + AdaptiveBatcher; weights on Ext1TB.
3. `search.py` core (refs, max-cos fusion, photo grouping) + CLI `search --photo/--box/--whole` with contact sheet; proof run on a ~2,000-photo real subset.

### Slice 2 — scene text, bibs, filters, evaluation
Depends on: slice 1.
Tasks:
1. `embed_scenes` stage + SigLIP2 text encoding; `--text` (person) and `--scene` query terms.
2. `ocr_bibs` stage (Apple Vision) + filters (time, photographer, album, bib) in `search.py` + CLI flags.
3. `eval --bib` command with recall report + contact sheet; tune default weights; record results in handoff.

### Slice 3 — local web UI
Depends on: slice 2.
Tasks:
1. FastAPI app: facets, query-photo upload → boxes, search, image/crop serving, labels, export.
2. Static `index.html` page (vanilla JS): upload, click box, filters, scene text, results grid with full-image view, me/not-me, export.
3. E2E: run `serve` on the real index, drive it in a browser, screenshot; fix what the real run exposes.

## Out of scope (v1)

Face recognition, clock-skew auto-alignment between photographers, multi-collection search, buying originals automatically, cloud hosting.
