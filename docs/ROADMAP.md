# Roadmap / status

Last updated 2026-09-28.

## Done
- yipai360 downloader: paced, resumable, single-instance, Retry-After aware (`src/photofinder/sources/yipai.py`, `scripts/download_yipai.sh`).
- Indexer: scan → detect (yolo26s) → embed_persons (OSNet x1_0 MSMT17 + SigLIP2 B/16) → embed_scenes (SigLIP2) → ocr_bibs (Apple Vision); memory-pressure-adaptive batching; per-collection lock.
- Search: fused clothing similarity (osnet 0.3 / siglip 0.7), person/scene text, filters (time, photographer, album, bib), me/not-me labels, CLI `search` and `eval`.
- Web UI (`photofinder serve`): bib / upload / click-a-person start points, Me/Not me, Find more, full-photo viewer, My photos + export.
- Measured quality (race925, 36 bibs): photo R@50 .354, cross-photographer R@50 only .153 — clothing search is a candidate generator; the bib → mark → Find more loop does the rest. Details: `docs/handoff/2026-09-27-photo-finder-search-design.md`.

## In progress (operational)
- FUGA 贡嘎100 (`data/yipai/83415673067642538672`, 68,488 photos) downloaded 2026-09-28 07:42; full index running (log: `data/yipai/83415673067642538672/index.log`). When it finishes, serve that collection instead of `data/subsets/race925`.
- Top-up: rerun `scripts/download_yipai.sh 83415673067642538672` a day or two after the race, then `photofinder index` again (incremental).

## Next (ranked)
1. **Cross-photographer recall** — biggest lever. Ideas: tighter crops / drop ghost & prop detections; stronger re-ID (CLIP-ReID, SOLIDER); part-based colour features (top / bottom / shoes / pack); use `eval --bib` across the 36 bibs as the benchmark.
2. Soft score boost for the user's own bib (reasoning for deferring in handoff S3-T1).
3. De-duplicate nested YOLO detections (box inside box) at index time.
4. Read bibs on uploaded query photos; don't return an already-indexed uploaded photo as its own #1.
5. Tune text/scene weights against labelled data (currently unvalidated defaults 0.5/0.5).

## Known small issues
- CLI contact sheet tracebacks on an unreadable result photo (web path handles it).
- A persistent Vision failure leaves persons OCR-pending forever with a repeating warning.
- `HF_HUB_OFFLINE` only engages after the first text query has fetched tokenizer files.
- `pyobjc-framework-Vision` arrives transitively via ocrmac; declare it directly.
- boxmot's OSNet download wrote `~/.cache/gdown/cookies.txt` once (weights themselves are on Ext1TB).
- Label counts drift between two open browser tabs until reload.
- Server startup transiently needs ~3× the float16 matrix (~2.7 GB at ~350k persons).
