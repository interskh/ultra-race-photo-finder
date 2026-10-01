# Roadmap / status

Last updated 2026-09-30.

## Done
- yipai360 downloader: paced, resumable, single-instance, Retry-After aware (`src/photofinder/sources/yipai.py`, `scripts/download_yipai.sh`).
- Indexer: scan → detect (yolo26s) → embed_persons (OSNet x1_0 MSMT17 + SigLIP2 B/16) → embed_scenes (SigLIP2) → optional `--ocr` bib reading (Apple Vision); memory-pressure-adaptive batching; each model stage in a spawned process capped at 4 GB (`--max-memory`), restarted fresh when it reaches the cap (details: `docs/handoff/2026-09-29-index-memory-cap.md`); per-collection lock.
- Search: fused clothing similarity (osnet 0.3 / siglip 0.7), person/scene text, filters (time, photographer, album, group, bib), me/not-me labels, CLI `search` and `eval`.
- Web UI (`photofinder serve`): bib / upload / click-a-person start points, Me/Not me, Find more, full-photo viewer, My photos + export.
- Saved people + originals (2026-09-28, `docs/superpowers/specs/2026-09-28-people-and-originals-design.md`): per-person marks ("Searching for" switcher, ✓ <name> / ✗ Not <name>), "matched via" thumbnails on Find more, My photos with per-photo original status, background originals download (progress, cancel, resume), zip, CSV, single-photo download in the viewer, M/N/←/→/Esc shortcuts. Real run: 10/10 originals downloaded from a copy of the live index.
- Races and albums, slices 1–3 of `docs/superpowers/specs/2026-09-29-multi-race-albums-design.md` (branch `implement-loop/2026-09-29-multi-race-albums-design`, not merged yet): Group filter (old yipai tag moves from album to group on first open), race registry `data/races.json` + `race add`, slug-or-path for index/search/eval/serve, multi-album scan (album title, `<platform>:<uid>` photographers), `race import` (one-time legacy move, forward-recoverable; rehearsed on a copy of the live 贡嘎 index), `photofinder download <race>` / `scripts/download.sh <race>` (yipai only), `download_yipai.sh` refuses registered order ids.
- Races and albums, slice 4: downloader base for new platforms (`sources/base.py`: catalog manifest, 403 re-list, breaker) + pailixiang adapter (`sources/pailixiang.py`: ak signing, OptTime paging, 1600px previews, shot time from the listing); `photofinder album add <race> <url> [--title]` (validates before any request, fetches the pailixiang title); `download` routes pailixiang albums to the new base.
- Races and albums, slice 6: race picker — one `photofinder serve` for every race (pick any indexed race, one loaded at a time, `/api/r/<race>/…`), last race and per-race person remembered in the browser, reload banner on a tab left on the old race.
- Races and albums, slice 7: Open on site — the photo viewer and My photos link every photo to its site (xxpie: the photo itself; yipai: the album, find it with the file-name search; pailixiang/photoplus: the album, find it by group, shot time and the file name shown with a Copy button); CSV `site_url`; photos from non-yipai albums show `open on site` and are skipped by Download originals. Links checked on the real sites for one photo per platform.
- Nearby shots (2026-09-30, `nearby.py`, `docs/handoff/2026-09-30-nearby-shots.md`): same-photographer shots next to confirmed photos (marked Me or exact bib hits) — "Next to your marked photos" section on Find more / bib results (±1–5, default 2, excluded from the ranked list via `exclude_photos`) and a ±3 filmstrip in the viewer (`,` / `.` step, marking re-anchors). Measured on bib reads (lower bounds): same photographer ±1 shot ~60% show the same runner, ±2 ~40%, ±3 ~25% (`uv run photofinder eval <race> --bib <bib> [--bib …]`, nearby table).
- Measured quality (race925, 36 bibs): photo R@50 .354, cross-photographer R@50 only .153 — clothing search is a candidate generator; the bib → mark → Find more loop does the rest. Details: `docs/handoff/2026-09-27-photo-finder-search-design.md`.

## In progress (operational)
- FUGA 贡嘎100 (`data/yipai/83415673067642538672`, 68,488 photos) downloaded 2026-09-28 07:42 and fully indexed 2026-09-28 (190,980 people; scenes done). Bib OCR finished 2026-09-29 for all 190,980 people (59,773 bib reads, 5,346 distinct): the last 130,884 (incl. ~40k left pending by the pre-fix Vision failures) took 2 h 9 min with the server off, footprint 470–940 MB, 0 Vision errors.
- 2026-09-28: fixed a Vision OCR leak (new VNRecognizeTextRequest per call → 22 GB footprint, 38 GB swap, then Vision Code=11 failures); OCR is now opt-in. After a top-up download, rerun with `--ocr` so new people get bibs.
- Top-up: rerun `scripts/download_yipai.sh 83415673067642538672` a day or two after the race, then `photofinder index` again (incremental).
- Live 贡嘎 migration done: the collection lives in `data/races/2026-gongga100/`; top-up is `scripts/download.sh 2026-gongga100`, then `photofinder index 2026-gongga100`.

## Next (ranked)
Races and albums slices 1–7 are done on the branch (spec above). Operational: 2026-chongli168 fully indexed 2026-09-29 (11,939 photos, 53,400 people; no `--ocr` yet); index 2026-siguniang once its download ends.

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
