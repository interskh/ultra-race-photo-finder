# Handoff — 2026-09-27-photo-finder-search-design

Slice 1 SLICE_BASE=ffe232d

## S1-T1 — config, memory, db schema, scan stage, CLI `index` skeleton

**Decisions**
- No EXIF `DateTimeOriginal` → `taken_at`/`taken_ts` NULL (orchestrator ruling: error-handling section beats the "fallback: file mtime" line; downloaded files' mtime is download time, not capture time).
- `taken_ts` = naive `datetime.strptime(...).timestamp()`, i.e. EXIF time interpreted in the indexing machine's local TZ. Only used for ordering/ranges; `taken_at` text is the canonical camera-local value.
- `photos.status` vocabulary: `ok` | `error` (+ `error` text `"<ExcType>: <msg>"`). Malformed/unparsable EXIF keeps the row `ok` with NULL time/camera so later stages still process it.
- All `*_at` columns are text `'YYYY-MM-DD HH:MM:SS'` local time (`stages.now()`); later stages should reuse `stages.now()`.
- Stage-done markers: `photos.scanned_at/detected_at/scene_done_at`, `persons.embedded_at/ocr_at` — later stages select `where <x>_at is null and status='ok'`.
- Image discovery: `os.walk` + suffix whitelist {.jpg,.jpeg,.png} case-insensitive + `is_file()` (follows file symlinks). This excludes `*.part`, `index.sqlite*`, `manifest.sqlite*`, `*-journal` for free. relpath/stem are the link's, not the target's.
- Manifest: read-only URI `file:...?mode=ro`, timeout=10, one LEFT JOIN query, closed via `contextlib.closing` before the walk (`with sqlite3.connect()` alone does not close).
- Rerun: preload `set(relpath)` and skip before opening the image; existing rows are never rewritten.
- RSS in memory logs is `ru_maxrss` (peak, bytes on macOS) labelled `max_rss`, stdlib only.
- `pressure_level()` returns normal on sysctl failure; any value ≥4 treated critical, ≥2 warn.
- `AdaptiveBatcher.next_size()` is the unit; `chunks(items)` is a convenience for task 2 stages. Logs on every size change, while paused, and every `log_every` (50) batches.
- `config.setup_model_env()` overwrites (not setdefault) HF_HOME, HF_HUB_CACHE, TORCH_HOME, YOLO_CONFIG_DIR, XDG_CACHE_HOME, MPLCONFIGDIR under `data/models/`; CLI calls it right after the mount check, before any stage runs. `device()` imports torch lazily.
- DB: WAL + foreign_keys on; schema defines all 7 tables now (plus indexes persons(photo_id), bibs(person_id)).

**Rejected**
- mtime fallback for `taken_at` (see above).
- psutil for current RSS: transitive, undeclared dep; stdlib adequate for now.
- `Image.verify()`/full decode at scan: slow on 10k+ files; truncated-but-valid-header JPEGs therefore scan as `ok` and will surface as errors in `detect`.
- Blacklist-based skip rules: whitelist is shorter and safer against new sidecar files.

**Assumptions**
- Collection photos are named `<photoId>.jpg` for the yipai join; other stems just get NULLs.
- Symlinked directories are not followed by `os.walk` (file symlinks are); the subset only uses file symlinks.
- macOS sysctl levels are 1/2/4 (checked: `sysctl -n kern.memorystatus_vm_pressure_level` → 1 today).

**Deferred**
- Detect/embed stages must set `status='error'` on decode failure (task 2).
- If current (not peak) RSS is needed to see model unloads, swap `max_rss_mb` for psutil (task 2 decision).
- CLI checks collection-is-dir before the mount check, so an unmounted disk with a collection path on it reports "not a directory" (still one line, exit 1).
- boxmot/open_clip weight dirs: verify they honor these env vars in task 2 (boxmot may default to its package dir).

**Touches**
- New: `src/photofinder/{config,memory,db,cli}.py`, `src/photofinder/index/{__init__,stages}.py`, `tests/test_memory.py`, `tests/test_scan.py`.
- `pyproject.toml`: `[project.scripts] photofinder = "photofinder.cli:main"` (plus orchestrator's deps + `uv.lock`).
- Shared surface: `index.sqlite` schema (`db.SCHEMA`), CLI subparser structure in `cli.main`.
- Real run wrote `data/subsets/first2000/index.sqlite` (gitignored).

## S1-T2 — models loaders, `detect` + `embed_persons` stages

**Decisions**
- Identifiers: YOLO `yolo26s.pt` (ultralytics 8.4.157, downloaded from assets v8.4.0 to `data/models/yolo26s.pt`); OSNet `osnet_x1_0_msmt17.pt` via `boxmot.reid.core.runtime.ReID(<abs path>, device=)` at `data/models/boxmot/` (Drive download worked); SigLIP2 `('ViT-B-16-SigLIP2', 'webli')` = HF `timm/ViT-B-16-SigLIP2` under `data/models/huggingface/hub` (1.4 GB). Dims 512 / 768, verified on real crops.
- Filtering: YOLO gets `classes=[0], conf=0.35, imgsz=1280`, and `models.filter_boxes` re-applies class/conf/height≥96 so the rule is testable without YOLO.
- `models.load_image` = full decode (`img.load()`) + `ImageOps.exif_transpose` + RGB; used by both stages, so boxes live in the EXIF-upright frame. That can differ from `photos.width/height` for rotated photos, but none of the first2000 subset is rotated (Orientation 1: 1,475, no tag: 525).
- Per-batch `with db:` covers error updates + persons inserts + `detected_at` (and emb inserts + `embedded_at`), so a crash rolls back the whole batch and nothing is duplicated.
- The loaders call `config.setup_model_env()` themselves, so other entrypoints (task 3, web) can't write to ~. I proved this was needed: a bare `import ultralytics` created `~/Library/Application Support/Ultralytics/settings.json`, which I then deleted.
- Normalization is applied twice: `embed_crops` returns L2-normalized float32 for query use, and the stage normalizes again in `to_blob` so a fake embedder test exercises it.
- OSNet and SigLIP2 are loaded together. Measured peak RSS was 2.46 GB (about 2.3 GB at the SigLIP load). YOLO is unloaded between stages: `cli` calls `models.unload()` after each stage.
- MPS output matched CPU detections exactly on 12 real photos (counts and confs).
- `memory` log now shows `rss=` (psutil current) plus `max_rss=`; psutil added as a direct dependency.
- Batch caps: detect 8 images, embed 64 crops.

**Real run** (first2000, device mps, `/usr/bin/time -l`): total 289 s real. detect 145.1 s (13.8 photos/s), 4,355 persons, 14 zero-person photos, 0 errors. embed_persons 141.2 s including about 10 s model load (30.8 persons/s), 4,355 embedded, blobs 1024 B / 1536 B. Peak RSS 2,455,928,832 B. Pressure samples: 17×normal, 12×warn, 0×critical. Warn was system-wide (process RSS stayed 160–970 MB): detect batch 8→1, embed 64→1, then grew back to 16. Rerun: 0.30 s, 38 MB RSS, `detect: 0 pending`, `embed_persons: 0 pending`, no `loaded` lines. After all loads, `~/.cache`, `~/Library/Application Support` and `~/.config/Ultralytics` were unchanged.

**Rejected**
- `boxmot.reid.create_reid_encoder(spec)`: it requires a pre-resolved artifact with sha256, which is heavier than `ReID(path)`.
- Leaving the class/conf filter to YOLO alone: the rule would then be untestable without the model.
- Chunking embed by photo: I chunk by person and cache images per chunk instead. A photo split across chunks is decoded twice, which is cheap at ≤1920 px.
- Stripping the SigLIP text tower to save RAM: slice 2 needs it.

**Assumptions**
- SigLIP preprocessing uses open_clip's own `squash` resize to 224 for tall crops. Query and index use the same path, so they stay consistent.
- Unreadable-at-embed photo → `photos.status='error'`. Its persons keep `embedded_at` NULL and drop out through the `status='ok'` join.

**Deferred**
- With `grow_after=20` at batch 1, recovery after long warn periods is slow. Detect ran mostly at batch 1 for this reason; retune if throughput matters.
- boxmot logs through Rich to stdout (noisy INFO lines). Left alone.

**Touches**
- New: `src/photofinder/models.py`, `tests/test_detect_embed.py`. Modified: `index/stages.py` (`detect`, `embed_persons`, `to_blob`, `error_text`), `cli.py` (stage loop + `models.unload()`), `memory.py` (`rss_mb`, log format), `tests/test_memory.py`, `tests/test_scan.py`, `pyproject.toml`/`uv.lock` (psutil).
- Public API for task 3: `models.load_image(path)`, `models.detect_persons(images) -> [[(x1,y1,x2,y2,conf)]]`, `models.crop(img, box)`, `models.embed_crops(crops) -> (osnet Nx512, siglip Nx768)` L2-normed float32, `models.l2norm`, `models.siglip() -> (model, preprocess)` (tokenizer: add `open_clip.get_tokenizer(SIGLIP[0])`), `models.unload()`, `stages.to_blob`.
