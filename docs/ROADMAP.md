# Roadmap / status

Last updated 2026-09-28.

## Done
- yipai360 downloader: paced, resumable, single-instance, Retry-After aware (`src/photofinder/sources/yipai.py`, `scripts/download_yipai.sh`).
- Indexer: scan → detect (yolo26s) → embed_persons (OSNet x1_0 MSMT17 + SigLIP2 B/16) → embed_scenes (SigLIP2) → optional `--ocr` bib reading (Apple Vision); memory-pressure-adaptive batching, 6 GB process-footprint stop; per-collection lock.
- Search: fused clothing similarity (osnet 0.3 / siglip 0.7), person/scene text, filters (time, photographer, album, group, bib), me/not-me labels, CLI `search` and `eval`.
- Web UI (`photofinder serve`): bib / upload / click-a-person start points, Me/Not me, Find more, full-photo viewer, My photos + export.
- Saved people + originals (2026-09-28, `docs/superpowers/specs/2026-09-28-people-and-originals-design.md`): per-person marks ("Searching for" switcher, ✓ <name> / ✗ Not <name>), "matched via" thumbnails on Find more, My photos with per-photo original status, background originals download (progress, cancel, resume), zip, CSV, single-photo download in the viewer, M/N/←/→/Esc shortcuts. Real run: 10/10 originals downloaded from a copy of the live index.
- Measured quality (race925, 36 bibs): photo R@50 .354, cross-photographer R@50 only .153 — clothing search is a candidate generator; the bib → mark → Find more loop does the rest. Details: `docs/handoff/2026-09-27-photo-finder-search-design.md`.

## In progress (operational)
- FUGA 贡嘎100 (`data/yipai/83415673067642538672`, 68,488 photos) downloaded 2026-09-28 07:42 and fully indexed 2026-09-28 (190,980 people; scenes done). Bib OCR stopped at 60,096 of 190,980 people by choice — the user doesn't need it (platforms already offer bib search); finish with `photofinder index <collection> --ocr` if ever wanted.
- 2026-09-28: fixed a Vision OCR leak (new VNRecognizeTextRequest per call → 22 GB footprint, 38 GB swap); OCR is now opt-in.
- Top-up: rerun `scripts/download_yipai.sh 83415673067642538672` a day or two after the race, then `photofinder index` again (incremental).

## Next (ranked)
1. **Cross-photographer recall** — biggest lever. Ideas: tighter crops / drop ghost & prop detections; stronger re-ID (CLIP-ReID, SOLIDER); part-based colour features (top / bottom / shoes / pack); use `eval --bib` across the 36 bibs as the benchmark.
2. Soft score boost for the user's own bib (reasoning for deferring in handoff S3-T1).
3. De-duplicate nested YOLO detections (box inside box) at index time.
4. Read bibs on uploaded query photos; don't return an already-indexed uploaded photo as its own #1.
5. Tune text/scene weights against labelled data (currently unvalidated defaults 0.5/0.5).

## Operational note — label migration
- The first open of the live index (`data/yipai/83415673067642538672`) by merged code migrates its labels into profile "Me" (one-way). The server on port 8000 still runs pre-merge code and cannot save marks after that: restart it on the merged code right away.

## Known small issues
- Originals carry the organizer's branding band (FUGA), same as the site's 下载 button; watermark detection for paid galleries is not implemented.
- Profile switcher uses the browser's prompt/confirm dialogs.
- CLI contact sheet tracebacks on an unreadable result photo (web path handles it).
- A persistent Vision failure leaves persons OCR-pending forever with a repeating warning.
- `HF_HUB_OFFLINE` only engages after the first text query has fetched tokenizer files.
- `pyobjc-framework-Vision` arrives transitively via ocrmac; declare it directly.
- boxmot's OSNet download wrote `~/.cache/gdown/cookies.txt` once (weights themselves are on Ext1TB).
- Label counts drift between two open browser tabs until reload.
- Server startup transiently needs ~3× the float16 matrix (~2.7 GB at ~350k persons).
