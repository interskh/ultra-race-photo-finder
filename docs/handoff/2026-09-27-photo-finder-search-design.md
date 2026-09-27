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

## S1-T3 — `search.py` core + CLI `search` + proof run

**Decisions**
- Scoring is `score(persons, refs, weights)`: terms = `(weight, max_cos)` for each refs key that has vectors and a weight, divided by the sum of those weights. Slice 2 adds a term by putting a key in `WEIGHTS` and a matrix in `Persons.vecs`, or by appending to `terms`.
- `load_persons` joins persons with both embedding tables (status `ok`) and fully loads them as float32. Vectors are re-L2-normalized after the float16→float32 cast, so a single-term score equals the exact cosine. The vector dim comes from the blob length (tests use 4/3-dim vectors).
- Grouping: a stable argsort of scores, taking the first (best) person per photo not in `exclude`, stopping at top-k.
- The CLI loads persons before any model, so "no embeddings" fails fast with no YOLO/SigLIP load. It checks `index.sqlite` exists before `db.connect`, which would otherwise create an empty index in the collection.
- Boxes are sorted by area descending, so box 0 is the largest. `--box`/`--whole` are mutually exclusive, and `--whole` never calls detect. YOLO is unloaded before the embed.
- Self-exclusion (`find_photo`) compares resolved paths. Candidates are only the rows whose filename equals the query's name or its resolved target's name, so this stays O(rows) string checks instead of resolving 350k paths.
- Contact sheet: tiles 256 px high, rows wrapped at 1800 px, PIL default font, label `#rank score id <source_photo_id>`. Default path is `config.DATA_ROOT/exports/<collection.resolve().name>-search-<ts>.jpg`, read at call time.
- Timing: `load_persons` logs its own load time and `search` logs scoring only (matmul + grouping).

**Rejected**
- Keeping float16 in memory and upcasting per chunk: the task says float32 matrices, and 4,355 persons is trivial. At 350k it would be about 1.8 GB (see Deferred).
- Resolving every indexed path for self-exclusion: correct but slow at 350k. It only misses a symlink whose name differs from both the query name and the target name.
- Putting the contact sheet in the CLI: it lives in `search.py` so slice 2 `eval` can reuse it.

**Assumptions**
- The `photos.source_photo_id` (yipai id) is the photo id shown to the user; the db id is the fallback.
- open_clip still sends a HEAD request to huggingface.co on SigLIP load (the model loads from the local cache). This was not changed here. Check: set `HF_HUB_OFFLINE=1` in `setup_model_env`.

**Proof run** (first2000, mps). Query `photos/9564992.jpg`: woman with a teal tie-dye shirt, lime vest and ONO visor, holding a big yellow "强者之路" sign.
- Boxes: `box 0 (35,499,1171,1779) conf .42`, `box 1 (33,244,1122,1410) conf .66`. Both are wide because they include the motion-blur ghosts. Self-exclusion line printed.
- `--box 0 --top 24`: 14.81 s real, scoring 0.008 s, persons load 0.07 s (separate run), peak RSS 2,242,576,384 B, pressure 1→1. Top 5: 4883138 .793, 5570749 .771, 5952110 .749, 5381856 .739, 6070672 .734. Sheet `data/exports/first2000-search-20260928-011830.jpg`.
- `--box 1`: 13.23 s, scoring 0.007 s, RSS 2,243,346,432 B, pressure 1→1. Top 5: 5381856 .792, 6070672 .780, 6383287 .779, 4883138 .775, 8987314 .749. Sheet `...-012033.jpg`.
- Second query `4357233.jpg` (the same woman, bib 8045, holding a bib card instead of the sign), `--box 0`: 16.26 s, scoring 0.004 s, RSS 2,242,084,864 B, pressure 1→1. Top 3: 2256534 .821, 1781084 .801, 8988981 .796. Sheet `...-012355.jpg`.
- **Verdict (honest): 0/10 of the top results were the same runner, in all three runs.** Results match the prop, pose and studio lighting (yellow sign, bib card held up, lime jackets), not the identity. Ground truth: 9564992 and 4357233 are the same person (found by photographer 阿光's burst ±1 min). They rank each other only #28–#30 fused. Per-term ranks were osnet #49–67 and siglip #40–165. A likely cause is that the boxes include the long-exposure ghost doubles and the handheld props, which dominate both embeddings on this 定妆照 studio set. Search mechanics are verified; identity quality on this subset is not. Slice 2 `eval`/weight tuning is where this gets measured and fixed. The CLI top-1 scores (.7930 / .7919) match the stored-embedding scores for the same boxes (.793 / .792 via `person_refs`), so query-time crop→embed reproduces index-time embeddings. The miss is model/data quality, not plumbing.
- Control query, check-in album: `5311971.jpg` (man in a purple shell jacket, black KUAI vest, pink/white sunglasses), `--box 0` (259,571,1053,1746) conf .86. 16.47 s real, scoring 0.004 s, RSS 2,243,461,120 B, pressure 1→1. Sheet `...-013848.jpg`. Top 10 were 5428287 .937, 8810720 .867, 5544637 .818, 1766462 .812, 6410157 .800, 6987448 .799, 1191162 .797, 9434797 .759, 5050583 .759, 3191046 .743.
- **Verdict: 2/10 are clearly the same runner (#1, #2, same burst), plus 1 plausible (#10, a studio shot 18 min later with the same jacket, vest and hair).** #3–#9 are different people in purple tops. The engine retrieves the same person when the crop is clean, and otherwise it ranks by dominant outfit colour. Studio 定妆照 queries fail, as described above.
- Rerun `photofinder index first2000`: `scan: 0 new (0 errors), 2000 already indexed`, `detect: 0 pending`, `embed_persons: 0 pending`, no `loaded` lines, 0.21 s, 40 MB RSS. The photos count stays 2000, with 0 relpaths matching search/export, so the three sheets in `data/exports` were not ingested.
- `~/Library/Application Support/Ultralytics` and `~/.config/Ultralytics` are absent. `~/.cache/huggingface` and `~/.cache/torch` are pre-existing symlinks to `/Volumes/Ext1TB/...`, and nothing in them is newer than the run.

**Deferred**
- float16-in-memory scoring for about 350k persons (target < 2 s). Change `matrix()` to keep float16 and upcast inside the `max_cos` chunk loop.
- Crop tightening and ghost/prop handling, plus weight tuning: slice 2 `eval`.
- A result photo that becomes unreadable between search and sheet rendering raises a traceback (not a listed failure path).

**Touches**
- New: `src/photofinder/search.py` (`Persons`, `Result`, `MissingEmbeddings`, `load_persons`, `person_refs`, `max_cos`, `score`, `best_per_photo`, `search`, `contact_sheet`, `find_photo`, `WEIGHTS`) and `tests/test_search.py` (18 tests).
- Modified: `src/photofinder/cli.py` (`search` subparser, `cmd_search`, `query_box`, `default_out`, `area`, `fmt_box`).
- Writes `data/exports/*.jpg` (gitignored).
- Mutations: 8/8 caught (dedupe, best-first order, renormalization, max→mean, self-exclusion, box sort, box range, default out path). Restored and checksum-verified.

## S1-gate fixes — whole-run gate round (4 defects)

**Decisions**
- Upright size: scan reads EXIF Orientation (0x0112) from the already-open header and swaps width/height for 5–8. This is the same rule as `ImageOps.exif_transpose` and needs no pixel decode.
- Manifest race: `find_images` runs first, then `load_manifest` does one short `mode=ro` read. Every file seen in the walk has its manifest row, because the downloader upserts rows before files.
- The "manifest closed" guarantee is now asserted at each `Image.open` (header read) instead of at walk start. The walk no longer follows the read, so the old premise did not hold.
- Contact sheet: after height normalization, any tile wider than the sheet is scaled down to the sheet width, so its height becomes less than 256. Row layout is unchanged.
- Self-exclusion: the basename prefilter is casefolded, and the candidate match uses `Path.samefile` (st_dev+st_ino). A new `same_file` helper returns False on OSError when an indexed file is missing.

**Rejected**
- Decoding plus `exif_transpose` in scan: correct, but it decodes every image and would break the header-only speed.
- Re-reading the manifest per file, or repairing NULL-join rows on rerun: more code and more manifest opens. Enumerate-then-read closes the race for everything that was walked.
- `os.path.normcase` for the case check: it is a no-op on macOS/posix.
- Comparing `resolve()` strings with a casefold: that would give false matches on case-sensitive volumes. Inode identity is exact.

**Assumptions**
- The downloader always commits a page's manifest rows before writing that page's files. A file written without any row stays NULL-joined, same as before.

**Deferred**
- Per orchestrator, the real first2000 subset has 0 rotated photos, so there was no reindex. Models and the real index were not rerun.
- Rows already scanned with raw (unrotated) dims or NULL joins are not backfilled, because rerun skips existing relpaths. This only matters for indexes built before this fix. The current subset is unaffected.
- A malformed EXIF block now makes scan mark the photo `error`, where before it recorded width/height only. `load_image`'s `exif_transpose` would fail on the same block at detect anyway.

**Touches**
- `src/photofinder/index/stages.py` (`scan`, `ORIENTATION` const) and `src/photofinder/search.py` (`contact_sheet`, `find_photo`, new `same_file`).
- `tests/test_scan.py`: the manifest test was renamed to `..._closed_before_image_reads`. Added `test_manifest_rows_added_during_walk_are_joined` and `test_exif_rotated_photo_stores_upright_size`.
- `tests/test_search.py`: added `test_find_photo_matches_case_variant_of_indexed_path` (skips on case-sensitive fs) and `test_contact_sheet_scales_wide_tile_to_fit`.
- Mutations: 5/5 caught (orientation swap, manifest order, width cap, casefold prefilter, samefile→resolve). Restored and sha256-verified.

## S1 whole-run gate (orchestrator)

- Proof setup: the subset `data/subsets/first2000/` holds 2,000 symlinks (the lowest photo_ids) into the live download dir and a one-time `sqlite3 .backup` snapshot of the live manifest, taken read-only with a 5 s timeout. I chose a snapshot over a symlink so the proof runs never touch the live manifest's locks again. The subset's albums are 9.25 签到 (1,315) and 定妆照 (685); it contains no race-course photos.
- Model identifiers:
  - `yolo26s.pt`
  - `osnet_x1_0_msmt17.pt` (boxmot 25.0.0 catalog, via `boxmot.reid.core.runtime.ReID`)
  - open_clip `('ViT-B-16-SigLIP2', 'webli')` = `timm/ViT-B-16-SigLIP2`
- Full suite: 58 passed before the fix round and 62 passed after it (`uv run pytest`). After the fixes, `photofinder index first2000` reran with 0 pending, loaded no model and took 0.19 s.
- Whole-run reviewer: APPROVE. Its 2 findings (the upright-dims frame mismatch and case-variant self-exclusion) were fixed in 74845b8. The blocker re-check was APPROVE, with all 4 fixes closed.
- Codex review had 6 findings:
  - Fixed in 74845b8: the manifest read-order race, contact-sheet clipping, and upright dims.
  - Rejected: "the `mode=ro` read takes a SHARED lock that can fail the downloader". The downloader's `sqlite3.connect` uses the default 5 s busy timeout (yipai.py:89), and our single read takes milliseconds.
  - Deferred: two concurrent `index` processes can duplicate persons. That is not a supported use; a fix would be an index lockfile.
  - Deferred: gdown hardcodes `~/.cache/gdown`. On the first OSNet download it rewrote `~/.cache/gdown/cookies.txt` (114 B; the directory already existed). The weights themselves landed on Ext1TB. A fix is to fetch the OSNet weights ourselves.
  - Codex was not rerun: the fixes were small and targeted at its own findings.
- Also deferred:
  - `HF_HUB_OFFLINE` is not set, so a SigLIP2 load sends a HEAD request to huggingface.co.
  - Scoring holds float32 embeddings (~1.8 GB at 350k persons) while the models are loaded.
  - Rows indexed before 74845b8 are not backfilled; first2000 has 0 rotated photos.
- Orchestrator contact-sheet verdicts:
  - `data/exports/first2000-search-20260928-011830.jpg` (query 9564992 box 0, a 定妆照 studio portrait holding a yellow 强者之路 sign): 0 of the top 10 are the same runner. The results match the sign, the ghosted studio lighting and the yellow clothing, not the person.
  - `…-013848.jpg` (query 5311971, a check-in photo: purple jacket, black vest, sunglasses, black tights): #1 and #2 are clearly the same runner, and #10 and #17 plausibly are (purple jacket with black vest). The rest are other people in purple tops.
  - Conclusion: search matches outfit colour well and identity only weakly. The mechanics are proven; ranking quality is for slice 2's `eval` and weight tuning.
- Timings: index 289 s for 2,000 photos (detect 145 s, embed 141 s, 4,355 persons), peak RSS 2.46 GB, pressure 17 normal / 12 warn / 0 critical samples. Search ~13–16 s wall (mostly model load), scoring <0.01 s, peak RSS 2.24 GB, pressure 1→1.

implement-loop: slice 1 shipped 74845b8; remaining: [2, 3]
