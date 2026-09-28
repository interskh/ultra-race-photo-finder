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

## Slice 2

Slice 2 SLICE_BASE=8109548

Race subset `data/subsets/race925/`: 5,745 file symlinks (album 9.25 赛事, tag_id 703070, all status done) into the live download dir + a read-only `sqlite3 .backup` snapshot of the live manifest taken 2026-09-28 (the first2000 snapshot predates the race album).

## S2-T1 — `embed_scenes` stage, SigLIP2 text encoding, `--text`/`--scene`, HF offline

**Decisions**
- `embed_scenes` mirrors `detect`: select `status='ok' and scene_done_at is null`, EXIF-upright `load_image`, one `with db:` per batch (error updates + scene inserts + `scene_done_at`), plain `insert` (the rollback makes duplicates impossible, and a PK clash would surface a bug instead of hiding it). Batch cap `SCENE_BATCH=16`. It runs after `embed_persons` in the CLI loop.
- `models.embed_images` (siglip preprocess → encode_image → l2norm) is now shared: `embed_crops` calls it for its siglip half. `models.encode_text` uses `models.tokenizer()`, which goes through `_get` so `unload()` drops it.
- Tokenizer is `open_clip.get_tokenizer(SIGLIP[0], tokenizer_type="gemma")`. Without `tokenizer_type`, transformers 5.17 `AutoTokenizer` calls `AutoConfig` for `config.json` (the repo has none). That works online but raises OSError under `HF_HUB_OFFLINE=1`, even with a `.no_exist/config.json` marker. With `gemma` the offline ids equal the online ids ("mountain" → 53074; "雪山" → 236722, 235822).
- New dependency: `transformers>=5.17.0`, which open_clip's `HFTokenizer` imports. The lock diff only adds packages (transformers, tokenizers 0.23.2, typer, shellingham, annotated-doc); no existing version changed. I synced only after the race925 index run had exited.
- HF offline predicate: `config.hf_cached` requires one snapshot of `models--timm--ViT-B-16-SigLIP2` to hold `open_clip_model.safetensors`, `tokenizer.json`, `tokenizer_config.json` and `special_tokens_map.json`. This deviates from the task's "dir exists" suggestion: before this task the cache held only the safetensors, so that check would have set offline and broken the first text query. It never pops an existing `HF_HUB_OFFLINE`.
- Search: `text` term = max cos(person siglip crop vec, text vec) via `SOURCES={"text":"siglip"}`. `scene` term = cos over per-photo scene vecs, broadcast to persons through a row index (`Persons.scene = (vecs, rows)`), so there is no 350k×768 duplicate.
- Photos with no scene vec (partial index) get a zero row, so scene cos = 0. They are not dropped, because the other terms may still rank them. An index with zero scene rows raises `MissingEmbeddings` naming embed_scenes.
- `load_scenes` runs only when a scene term is requested. `search()` lazy-loads it, and the CLI calls it before any model load, so it fails fast.
- Weights: `text 0.5, scene 0.5`, equal to the spec's `w_reid = w_clip` defaults. Real numbers: text↔image cos spans ~0.01–0.11 while image↔image cos spans ~0.83–0.88 on the same photos. So with a photo ref, text/scene move the fused rank only slightly at 0.5. S2-T3 `eval` should tune this; solo text/scene queries are unaffected because they are renormalized.
- Scene-only (and text-only) ranking is per person: photos with zero detected persons are never returned. This is accepted because race photos nearly always have a runner.
- CLI: `--photo` is optional. With none of `--photo/--text/--scene` it exits with one line, before the index check. Text and scene are encoded in one `encode_text` call. Text/scene-only runs load only SigLIP and its tokenizer (verified in the real run's `loaded` lines). The contact sheet has no query tile when there is no `--photo`. `--box/--whole` are silently ignored without `--photo`.

**Rejected**
- `transformers[sentencepiece]`: not needed, because the fast tokenizer loads from `tokenizer.json`.
- Setting offline when the repo dir merely exists: it breaks the tokenizer download (see above).
- Materializing scene vecs per person, or an inner join dropping persons without scene vecs: memory at 350k, and silent result loss on partial indexes.
- Prompt templates ("a photo of {}") for scene text: raw text keeps it predictable. Revisit in S2-T3 if recall is poor.

**Assumptions**
- The HF repo `timm/ViT-B-16-SigLIP2` tokenizer file set stays the four files above. If a future transformers wants another file offline, delete one of the four from the snapshot to force the online path once.
- GemmaTokenizer (`tokenizer_type="gemma"`) matches the repo's `tokenizer_class`. Check: `tokenizer_config.json` → `"tokenizer_class": "GemmaTokenizer"`.

**Real check** (after `pgrep` was clear; race925 index already built by the orchestrator, `embed_scenes` NOT run on it)
- First load (online) downloaded the tokenizer files (34 MB) in 99 s. After that `HF_HUB_OFFLINE` env = 1, `huggingface_hub.constants.HF_HUB_OFFLINE` = True, and model plus tokenizer load offline in 13 s wall with peak RSS 2.04 GB. `~/.cache` listing was unchanged across both runs.
- `encode_text(["mountain","雪山","finish arch","night city street","forest trail at night"])` → (5,768), norms 1.0. cos(mountain, 雪山) = 0.90.
- Scene vecs for 3 race925 photos (all night shots) had norm 1.0. On "night city street", the street photo 2531911 scored .041 vs .015/.013 for the trail photos. On "forest trail at night", the trail photos 72443666/7637494 scored .114/.100 vs .049. 雪山 is lowest on the street photo (.011).
- `photofinder search race925 --text "red jacket" --top 8`: 14.1 s, peak RSS 2.26 GB, 26,783 persons. All 8 tiles are people in red tops/jackets. `--scene mountain` → one-line "no scene embeddings (stage embed_scenes)", with no model load. No query flags → one-line error.
- First-download path with the shipped `tokenizer_type="gemma"`: `setup_model_env(<empty scratch dir>)` left offline unset, the four tokenizer files downloaded, and the ids matched. I deleted the scratch dir afterwards.
- `--photo race925/photos/2531911.jpg --top 5`: this exercises the refactored `embed_crops`. It printed 9 boxes and self-excluded the query. #1 was 5624888 (.880, same photographer, 2 s earlier). Peak RSS 2.17 GB.
- Mutations: 9/9 caught (scene→crop vecs, broadcast by photo_id order, text→osnet, no renormalization, offline always, offline on any file, `scene_done_at` not set, no-query check removed, scenes loaded after models). Restored from saved copies and sha256-verified.

**Deferred**
- Contact-sheet labels overlap on narrow tiles (pre-existing, cosmetic).
- `embed_scenes` was not run on race925; the orchestrator's next `index` does it for all 5,745 photos.

**Touches**
- `src/photofinder/{config,models,search,cli}.py`, `src/photofinder/index/stages.py` (`embed_scenes`, `SCENE_BATCH`), `pyproject.toml` + `uv.lock` (transformers).
- Tests: new `tests/test_config.py`; `tests/test_search.py` (+10 test cases, `Fakes.encode`), `tests/test_detect_embed.py` (+5 tests, index-rerun test fakes `embed_images`), `tests/test_scan.py` (fakes `embed_images`).
- Public API: `models.embed_images`, `models.encode_text`, `models.tokenizer`, `search.load_scenes`, `search.term_scores`, `search.SOURCES`, `Persons.scene`, `WEIGHTS` keys `text`/`scene`, `config.hf_cached/SIGLIP_REPO/SIGLIP_FILES`.

## S2-T2 — `ocr_bibs` stage, filters (time/photographer/album/bib), CLI flags, index lockfile

**Decisions**
- OCR: `ocrmac` 1.0.1 Vision backend, `recognition_level="accurate"`, `language_preference=["en-US"]`, one pass per person. `models.read_text(img) -> [(text, conf)]` imports ocrmac lazily.
- Crop policy (`stages.read_bibs`): below 200 px tall, skip OCR but still stamp `ocr_at`. Below 700 px, upscale ×2 with LANCZOS. At 700 px and above, use ×1.
- Token rule: `(?<!\w)[0-9]{3,5}(?!\w)`, deduped per person with the max conf. Leading zeros are kept. "No.1685"/"#8038"/"15/817" → the digits. Letter-glued runs ("-l2100", "157H1E7", "i1111", "109g"), runs over 5 digits and fullwidth digits are dropped.
- Stage mirrors `embed_persons`: a per-chunk image cache, one `with db:` per batch (error updates + bibs + `ocr_at`), `OCR_BATCH=64` AdaptiveBatcher, injectable `reader`. It runs after `embed_scenes` in the CLI loop.
- Filters: `search.Filters(start, end, photographers, albums, bib)`. `filter_mask` builds one SQL query and `np.isin`s it into a person mask. `search()` scores all persons, then `best_per_photo` runs over `flatnonzero(mask)`. Time bounds are inclusive text comparisons, so NULL `taken_at` drops out only when a bound is set. Photographer matches `photographer` or `photographer_uid`. Album is exact. Bib uses `instr` (substring) on any of the person's bibs rows.
- Filters are AND-combined; repeated `--photographer`/`--album` values are OR-ed within a flag. `--to 10:00` means ≤ 10:00:00.
- `check_filters` raises `MissingEmbeddings` naming `ocr_bibs` when `--bib` is used and no person has `ocr_at`. The CLI calls it right after `load_persons`, before any model load.
- CLI: `--from/--to` accept `YYYY-MM-DD HH:MM[:SS]`, normalized to `:SS`. A bad value exits with one line before the index check. An empty result prints "no photos match the filters" (or "no results" with no filters), writes no sheet, and exits 0.
- Lock: `cmd_index` holds `fcntl.flock(LOCK_EX|LOCK_NB)` on `<collection>/index.lock` before `db.connect`. If held, it exits 1 with one line; no stage runs and no index is created. `.lock` is not in the scan suffix whitelist. The file is left in place (0 B), and flock releases on process exit.
- Dependency: the `uv add ocrmac` lock diff was additive only (ocrmac 1.0.1, pyobjc-core and pyobjc-framework-{cocoa,coreml,quartz,vision} 12.2.2). `uv sync --inexact` ran while the orchestrator's race925 index was active; it only added packages and re-linked the editable photofinder. The running process had already imported its modules.

**OCR probe** (race925, read-only; random persons by height bin; tokens are "crop has ≥1 3–5 digit run")
- Probe 1, ×1 vision vs livetext: <400 px 0/42 for both. 400+ px 3/14 for both, and each missed one the other got. Livetext is not better and cannot set a level. Conf is not always 1.0: 0.3/0.5/1.0 were seen.
- Probe 2/3, ×1 vs ×2:
  - 200–300 px: 0/20 vs 0/20.
  - 250–400 px: 1/40 vs 2/40. 300–400 px: 1/20 vs 3/20.
  - 400–700 px: 4/40 vs 7/40. Every gain was checked visually as a real bib (8004, 8001, 8014, 8041, 8030, 8015), with no losses.
  - 700+ px: 17/40 vs 20/40, at ×3 the time. ×2 degraded real reads there (8016→016, 8040→040, 8013 lost) and added watermark garbage.
- 96–200 px: 1/30 at both scales, and that one was watermark garbage (贡嘎100 → 15100).
- Height distribution of race925's 26,783 persons: <200: 3,220; 200–700: 15,628; 700+: 7,935.
- Real `photofinder index` on a 10-photo, 88-person copy (markers preset so no torch model loads): 24 bibs on 23 persons, including all 9 seeded bibs (8006, 8036, 8017, 8004, 8041, 8043, 8015, 8030, 8040) plus 8038.
  - Timing: cold 11.3 s (Vision first load), warm 3.3 s = 38 ms/person, peak RSS 296 MB. Estimate for race925: about 17 min.
  - Rerun: `ocr_bibs: 0 pending`. A second concurrent `photofinder index` was refused (exit 1), and the first run finished with no duplicate bibs.

**Rejected**
- Livetext: no gain in probe 1, and it cannot take `recognition_level`.
- Two passes (×1 + ×2, union): 2–3× cost, and at 700+ the ×2 pass adds garbage. A single height-dependent scale captured the ×2 gains.
- A plain `(?<!\d)` digit boundary: it keeps letter-glued watermark garbage ("157H1"→157, "-l2100"→2100). A whitespace-split rule would have dropped "No.1685".
- argparse `type=` for times: it prints usage plus an error (2 lines), unlike the other one-line exits.
- `lockf`/`F_SETLK`: POSIX locks are per process, so a second fd in the same process would not conflict.

**Assumptions**
- Watermark digit runs stay as false positives for the `--bib` substring filter (e.g. "80001", "80002" from 传棋越7/贡嘎100). Check: `select text, count(*) from bibs group by text order by 2 desc` after the full run.
- `/Volumes/Ext1TB` is APFS (checked with `mount`), so flock behaves as on tmp_path.

**Deferred**
- bib_bonus: soft boost for a known own bib, deferred to slice 3 UI.
- `ocr_bibs` was not run on race925; the orchestrator's next `index` does it (~17 min).
- ocrmac PNG-encodes every crop (~12 ms/call, about 30% of warm time). Acceptable at this scale.
- `ocrmac` (pyobjc) is an unconditional dependency, so the project now installs only on macOS. This matches the design's Apple Vision choice.
- The race925 `index` process that was running during this task started from pre-lock code, so it holds no `index.lock`. Only runs started from this commit on exclude each other.

**Touches**
- `src/photofinder/models.py` (`read_text`), `index/stages.py` (`bib_tokens`, `read_bibs`, `ocr_bibs`, `OCR_*`, `BIB_TOKEN`), `search.py` (`Filters`, `check_filters`, `filter_mask`, `search(filters=)`), `cli.py` (`lock_index`, `LOCK_NAME`, `parse_time`, `--from/--to/--photographer/--album/--bib`, `ocr_bibs` in the loop), `pyproject.toml` + `uv.lock`.
- Tests: new `tests/test_ocr_bibs.py` (21, incl. one real Vision read of rendered "0887"); `tests/test_search.py` +14; `tests/test_scan.py` +2 lock tests (second fd, subprocess holder); both index CLI tests now fake `read_text`.
- Writes `<collection>/index.lock` on every `index` run.
- Mutations: 14/14 caught. They were: NULL-time kept, bib exact, photographer uid ignored, photographer nickname ignored, lock blocking (hung → timeout), lock never taken, `ocr_at` only with tokens, digits {3,6}, digits {4,5}, digit-only boundary, no min height, no upscale, no empty-result guard, CLI bib check removed. Files were restored from saved copies and sha256-verified.

### S2-T2 fix round (review findings)
- `--to 'YYYY-MM-DD HH:MM'` now means `:59`, so it includes the whole minute; `--from` stays at `:00`. An explicit `:SS` is used as given. This supersedes "`--to 10:00` means ≤ 10:00:00" above.
- `--bib ""` or whitespace-only → one-line error. Other `--bib` values are stripped.
- `check_filters` now returns a warning, `"ocr_bibs incomplete: N of M persons not read yet; rerun `photofinder index`"`, when some persons in `ok` photos have NULL `ocr_at`. The CLI prints it and continues. It still raises when no person has been read. Only persons in `status='ok'` photos are counted (before this round: any person).
- The CLI filter test was replaced by a parametrized per-flag test. Each of `--from`, `--to`, `--photographer`, `--album` and `--bib` alone changes the result on the fixture, plus one combined case. Added tests for the whole-minute rule, empty bib and partial-OCR warning.
- Rejected: logging the warning inside `filter_mask`. The CLI calls both `check_filters` and `search`, so it would print twice; returning the string lets the slice 3 web app show it too.
- Mutations: 9/9 caught. They were: each of `--from`/`--to`/`--photographer`/`--album` replaced by an empty value (the reviewer's exact mutation), empty bib accepted, warning never built, warning not printed, `--to` keeps `:00`, `--from` also gets `:59`. Restored and sha256-verified.
- `uv run pytest`: 127 passed.

## S2-T3 — `photofinder eval --bib`, tuned default weights, scene-only sheet tiles

**Decisions**
- New `evaluate.py` holds the pure logic (`ground_truth`, `recall`, `rank_photos`, `evaluate_ref`, `evaluate_bib`, `mean_over_bibs`, `frequent_bibs`, `sheet`); `cli.cmd_eval` does the wiring. It calls `search.score`/`best_per_photo` directly, not `search.search()`, which would do 50 db lookups per ref.
- GT = `status='ok'` photos with a person whose `bibs.text = ?` (exact match). Refs = the largest-area bib-b person per GT photo, restricted to persons with embeddings. Ordered by photo id ascending, capped by `--refs 20`.
- Photographer key = `coalesce(photographer_uid, photographer)`. Cross-photographer set = GT′ photos with a known key that differs from the ref photo's key. A ref with an empty cross set is skipped for xR@50 only (`xrefs` column).
- Mean over bibs = mean of the per-bib means (each bib weighted equally; `nanmean` for xR@50). It is not pooled over refs.
- `evaluate.KS = (10, 50)` is read at call time so tests can shrink k. `configs()` adds a `default o:s` row only when `search.WEIGHTS` is not proportional to a listed config.
- No-OCR check reuses `search.check_filters(conn, Filters(bib=...))`. It raises naming `ocr_bibs` and prints the partial-OCR warning. Order: `load_persons` (embed_persons message), then OCR. The eval path never calls `models._get`.
- Fallback: with no `--bib`, or when a bib has <2 GT photos or no embedded ref, eval prints one line and then the top 20 bibs, 4-digit texts first (`order by length(text)=4 desc, #photos desc, text`). The race925 top raw tokens are watermark garbage ("157" 41 photos, "100" 30), so 4-digit-first makes the list useful. Valid bibs in the same run are still evaluated.
- Contact sheet: the first ref under the default weights, with the query crop plus the top 30. GT′ hits get a green border and "BIB" in the label. Labels have 3 lines (`#rank score [BIB]` / `id <source id>` / `ocr <texts>`) with `label=40`, which fixes the overlapping-label problem on narrow tiles for eval sheets. The path is `default_out(collection, f"eval-{bib}", --out-dir)`, and `default_out` gained `kind`/`out_dir` args.
- `search` scene-only (no `--photo`, no `--text`) tiles are now the whole photo; `contact_sheet` downscales them to 256 px.
- **Default weights: osnet 0.3 / siglip 0.7** (was 0.5/0.5). It has the best mean R@50, R@10 and xR@50 over 36 bibs. A finer ad-hoc sweep (`evaluate_bib` over the same 36 bibs with osnet 0.0–1.0 step 0.1; not shipped) peaks at 0.3:0.7 for R@50/R@10: 0.2:0.8 .352/.252, 0.3:0.7 .354/.253, 0.4:0.6 .353/.246 (xR@50 .148/.153/.155). Text/scene weights are unchanged because there is no eval evidence for them.
- `test_scene_query_without_scene_embeddings_names_stage` relied on an exact osnet/text tie under 0.5/0.5. It now passes explicit weights `{osnet:1, text:.5}`, which gives a real ordering.

**Rejected**
- Pooled mean over refs: bibs with 20 refs would dominate those with 10.
- Listing all tokens by frequency in the fallback: watermark tokens fill the top.
- Picking osnet:siglip from bib 8038 alone: 8038 favours 0.5:0.5 (R@50 .242 vs .220), but it is one bib of 36.
- Changing `contact_sheet` itself for label wrapping: eval passes multi-line labels and a taller label band, so search sheets are unchanged.

**Assumptions**
- OCR false positives pollute GT. Bib 8020's first ref (photo 64421025) visibly wears **8029**; results #1/#2 are the same runner (OCR 8029). That explains 8020's near-zero recall. Recall also counts only photos where the bib is visible and read, so same-runner hits with hidden bibs count as misses (e.g. 8039 sheet #3 5180772 looks like the query runner with no OCR).
- R@50 counts hits in the top 50 *photos*. With 10–27 GT photos per bib, R@50 differences of ~.01 are within noise (one photo for one ref).

**Deferred**
- Tuning text/scene weights (no labelled scene/text ground truth); bib_bonus/w_neg (slice 3 labels).
- A GT sanity filter (e.g. require ≥2 OCR reads, or conf ≥ x) to drop false-positive refs like 8020.

**Real run** (race925, 26,783 persons, worktree code verified via `photofinder.__file__`)
- `uv run photofinder eval …/race925 --bib 8038`: persons load 0.8 s, total ≈1 s, no model load.
- 36-bib run (`--bib` for every `length(text)=4` bib with ≥10 distinct ok photos): 15.8 s wall. It wrote 36 sheets.
- Fallback check: `--bib 157 --bib 77777` evaluated 157 (R@50 ≈ .02, garbage GT), printed "bib 77777: 0 photo(s)…", then listed 8020 27/5, 8039 17/5, 8010 16/4, …
- Sheets (tuned weights): 8038 `data/exports/race925-eval-8038-20260928-040841.jpg` (0 GT hits in top 30 for ref 3651984). A GT photo whose bib person has no embedding is a guaranteed miss; race925 has none, 8039 `…-eval-8039-20260928-040842.jpg` (#1/#2 green GT hits, #3 plausible same runner), 8020 `…-eval-8020-20260928-040842.jpg` (polluted GT, see above), 8010 `…-eval-8010-20260928-040843.jpg`.

Columns: R@50 per config, then R@10 and xR@50 for the chosen 0.3:0.7. refs = GT for every bib (cap 20 hits only 8020).

| bib | GT | refs | R@50 osnet | siglip | 0.3:0.7 | 0.5:0.5 | 0.7:0.3 | R@10 0.3:0.7 | xR@50 0.3:0.7 |
|---|---|---|---|---|---|---|---|---|---|
| 8020 | 27 | 20 | 0.017 | 0.050 | 0.033 | 0.027 | 0.019 | 0.013 | 0.023 |
| 8039 | 17 | 17 | 0.210 | 0.217 | 0.257 | 0.239 | 0.239 | 0.199 | 0.150 |
| 8010 | 16 | 16 | 0.350 | 0.229 | 0.362 | 0.371 | 0.375 | 0.213 | 0.067 |
| 8027 | 15 | 15 | 0.357 | 0.395 | 0.438 | 0.429 | 0.381 | 0.310 | 0.230 |
| 8012 | 14 | 14 | 0.170 | 0.181 | 0.170 | 0.176 | 0.181 | 0.148 | 0.000 |
| 8024 | 14 | 14 | 0.434 | 0.445 | 0.522 | 0.511 | 0.473 | 0.445 | 0.120 |
| 8005 | 13 | 13 | 0.231 | 0.340 | 0.378 | 0.321 | 0.301 | 0.256 | 0.187 |
| 8016 | 13 | 13 | 0.250 | 0.167 | 0.301 | 0.308 | 0.301 | 0.218 | 0.147 |
| 8046 | 13 | 13 | 0.718 | 0.718 | 0.776 | 0.769 | 0.744 | 0.654 | 0.157 |
| 8047 | 13 | 13 | 0.244 | 0.301 | 0.308 | 0.282 | 0.269 | 0.212 | 0.109 |
| 8025 | 12 | 12 | 0.220 | 0.250 | 0.288 | 0.265 | 0.235 | 0.242 | 0.000 |
| 8026 | 12 | 12 | 0.250 | 0.167 | 0.242 | 0.242 | 0.242 | 0.174 | 0.012 |
| 8028 | 12 | 12 | 0.273 | 0.295 | 0.356 | 0.333 | 0.311 | 0.235 | 0.236 |
| 8035 | 12 | 12 | 0.091 | 0.136 | 0.136 | 0.114 | 0.106 | 0.068 | 0.061 |
| 8036 | 12 | 12 | 0.364 | 0.394 | 0.439 | 0.409 | 0.371 | 0.326 | 0.356 |
| 8038 | 12 | 12 | 0.212 | 0.136 | 0.220 | 0.242 | 0.227 | 0.167 | 0.067 |
| 8002 | 11 | 11 | 0.609 | 0.582 | 0.791 | 0.773 | 0.727 | 0.582 | 0.612 |
| 8011 | 11 | 11 | 0.336 | 0.218 | 0.273 | 0.345 | 0.355 | 0.227 | 0.000 |
| 8021 | 11 | 11 | 0.200 | 0.336 | 0.309 | 0.245 | 0.236 | 0.209 | 0.158 |
| 8023 | 11 | 11 | 0.218 | 0.455 | 0.473 | 0.391 | 0.336 | 0.236 | 0.485 |
| 8040 | 11 | 11 | 0.264 | 0.309 | 0.336 | 0.309 | 0.282 | 0.255 | 0.159 |
| 8042 | 11 | 11 | 0.518 | 0.518 | 0.527 | 0.527 | 0.527 | 0.527 | 0.000 |
| 8044 | 11 | 11 | 0.355 | 0.445 | 0.545 | 0.518 | 0.464 | 0.336 | nan |
| 8003 | 10 | 10 | 0.244 | 0.367 | 0.333 | 0.333 | 0.322 | 0.244 | 0.000 |
| 8007 | 10 | 10 | 0.056 | 0.067 | 0.056 | 0.056 | 0.067 | 0.044 | 0.011 |
| 8008 | 10 | 10 | 0.089 | 0.256 | 0.222 | 0.156 | 0.122 | 0.122 | 0.039 |
| 8009 | 10 | 10 | 0.322 | 0.244 | 0.300 | 0.322 | 0.333 | 0.244 | 0.050 |
| 8013 | 10 | 10 | 0.456 | 0.533 | 0.678 | 0.611 | 0.522 | 0.433 | 0.580 |
| 8018 | 10 | 10 | 0.344 | 0.356 | 0.422 | 0.411 | 0.378 | 0.289 | 0.370 |
| 8019 | 10 | 10 | 0.278 | 0.267 | 0.411 | 0.378 | 0.311 | 0.189 | 0.300 |
| 8022 | 10 | 10 | 0.189 | 0.233 | 0.267 | 0.311 | 0.267 | 0.178 | 0.071 |
| 8031 | 10 | 10 | 0.144 | 0.300 | 0.333 | 0.300 | 0.244 | 0.133 | 0.234 |
| 8032 | 10 | 10 | 0.211 | 0.278 | 0.233 | 0.233 | 0.222 | 0.189 | 0.069 |
| 8033 | 10 | 10 | 0.444 | 0.333 | 0.444 | 0.467 | 0.478 | 0.356 | 0.060 |
| 8034 | 10 | 10 | 0.256 | 0.211 | 0.300 | 0.300 | 0.267 | 0.256 | 0.096 |
| 8037 | 10 | 10 | 0.244 | 0.133 | 0.278 | 0.278 | 0.278 | 0.189 | 0.149 |
| mean over 36 bibs | 434 | 427 | 0.282 | 0.302 | 0.354 | 0.342 | 0.320 | 0.253 | 0.153 |

Bib 8038 full row set (R@10/R@50/xR@50): osnet .144/.212/.081, siglip .091/.136/.019, 0.3:0.7 .167/.220/.067, 0.5:0.5 .174/.242/.093, 0.7:0.3 .167/.227/.090 (12 refs, 12 GT).
Mean over 36 bibs (R@10/R@50/xR@50; 427 refs, 416 xrefs): osnet .214/.282/.090, siglip .200/.302/.102, **0.3:0.7 .253/.354/.153**, 0.5:0.5 .243/.342/.146, 0.7:0.3 .235/.320/.122.

**Tests / mutations**
- `uv run pytest -q tests/test_search.py`: 68 passed (+14: eval GT/refs, recall, ref exclusion + cross split, configs change ranking, mean over bibs, frequent listing, CLI eval + no-model, sheet labels/hits, 3 fallback cases, no-OCR, no-embeddings, scene-only tiles). `uv run pytest -q`: 141 passed.
- Mutations: 8/8 caught (substring GT, ref photo not excluded, GT′ denominator includes ref photo, no cross-photographer filter, scene-only tiles cropped, ref = smallest box, 4-digit-first dropped, sheet marks every result). Restored from saved copies; sha256 verified.

**Touches**
- New `src/photofinder/evaluate.py`. `src/photofinder/cli.py` (`eval` subparser, `cmd_eval`, `print_rows`, `print_frequent`, `default_out(kind, out_dir)`, scene-only tiles). `src/photofinder/search.py` (`WEIGHTS` osnet .3 / siglip .7). `tests/test_search.py`.
- Shared surface: `search.WEIGHTS` default changes every photo search's ranking; `default_out` signature (backward compatible).

### S2-T3 correction — strict person-level recall (orchestrator finding)
- Flaw in the tables above: GT hits are photo-level. A crowded start-line photo counted as a hit even when its best-scoring (returned) person was someone else, e.g. on the 8010 sheet #9 5865973 is the OCR-8035 runner. The photo-level R@10/R@50/xR@50 above are unchanged and kept: they are the user-facing "my photo came back" number.
- Added strict sR@10/sR@50: a GT′ photo counts only if its best-scoring person, the box returned, is in `Truth.persons`, the set of all persons whose OCR text equals b (not only the refs). Same denominator |GT′|.
- Sheet: strict hits get a green border and "BIB"; photo-level-only hits get an orange border and "bib-photo".
- Strict agrees with photo-level, so **the osnet 0.3 / siglip 0.7 choice stands** (primary strict R@50, tie-break photo R@50).
- Mean over 36 bibs (R@10/R@50/xR@50/sR@10/sR@50): osnet .214/.282/.090/.209/.268, siglip .200/.302/.102/.194/.285, **0.3:0.7 .253/.354/.153/.248/.341**, 0.5:0.5 .243/.342/.146/.238/.329, 0.7:0.3 .235/.320/.122/.230/.306.
- Fine sweep sR@50: 0.2:0.8 .338, 0.3:0.7 .341, 0.4:0.6 .340, 0.5:0.5 .329.
- Bib 8038: strict equals photo-level for every config (no crowd-shot hits), e.g. 0.3:0.7 .167/.220/.067/.167/.220.
- Crowd inflation is visible on some bibs, e.g. 8010 at 0.3:0.7 R@50 .362 vs sR@50 .304.
- Fresh sheets: 8038 `data/exports/race925-eval-8038-20260928-042725.jpg`, 8039 `…-eval-8039-20260928-042726.jpg`, 8010 `…-eval-8010-20260928-042727.jpg`. The 8010 sheet was checked visually: #7 and #9 are orange bib-photo, #18 is green BIB.
- Tests: +1 (`test_strict_hit_needs_the_matched_person_to_carry_the_bib`). The eval fixture's photo 2 gained a non-bib person that out-scores the bib person under osnet. `uv run pytest -q`: 142 passed.
- Mutations (whole battery rerun): 11/11 caught. The 3 new strict mutants were: strict = photo-level, strict persons = refs only, sR@50 computed from the photo-level ranking. The sheet mutant is now "every GT photo marked strict". Files were restored from saved copies and sha256-verified.
- Touches: `evaluate.py` (`Truth.persons`, `Row.s10/s50`, `evaluate_ref` returns 5 values, sheet marks), `cli.print_rows` (2 new columns), `tests/test_search.py`.

## S2 orchestrator — real runs and visual verdicts

**race925 index** (5,745 photos, album 9.25 赛事, device mps). Run from committed code or a byte-identical snapshot of it:
- scan 3.4 s; detect 412.2 s (26,783 persons, 0 errors); embed_persons 671.0 s. Total 1,086.7 s, max_rss 1.97 GB, pressure samples 109 warn / 0 critical.
- embed_scenes 192.5 s (5,745 photos, 0 errors), max_rss 2.11 GB.
- ocr_bibs 1,156.0 s (26,783 persons, 5,101 bib tokens, 0 errors), peak footprint 852 MB. The S2-T2 fix round changed neither `stages.py` nor `models.py`, so the run matches the committed OCR code (diff-checked).
- Bib 8038 exists in race925: 12 photos, 4 photographers. The user's suggested bib was evaluated directly.

**Scene queries** (acceptance #4):
- race925 is a night race (19:59–00:00) and contains no real mountain photos. "mountain" and "雪山" put 16/16 start-stage photos with the giant snow-mountain LED backdrop on top, which is the only mountain imagery in the set. The bottom of the ranking is dark village streets and trails. That is correct, but it is not a real test.
- For a real test I built `data/subsets/scenemix/`: 800 symlinks, 400 random 9.26 赛事 photos and 400 random 9.25 赛事 photos, plus a manifest snapshot. Its index has 4,047 persons, built in 206.5 s.
- Whole-photo scene cosine (scratch script) gave:
  - "mountain": top 16 are 16/16 real Gongga sunrise mountain photos.
  - "雪山": 16/16.
  - "finish arch": the top 6 are the finish arch, and mountain photos rank at the very bottom (797–800 of 800).
- CLI `search scenemix --scene 雪山 --top 12` (per-person ranking, whole-photo tiles) ranked:
  - #1–4: runners in front of snow peaks;
  - #5–7: stage/arch backdrops with mountain graphics;
  - #8–12: runners crossing a boulder river in a mountain valley.
- Verdict: pass. Mountain photos rank above finish-arch and night-stage photos. Landscape photos with no detected person are not returned by design (S2-T1 decision).

**Eval contact sheets (orchestrator visual check, default 0.3/0.7):**
- 8038 (`race925-eval-8038-20260928-040841.jpg`): the query is a man with glasses, a headlamp and a blue vest over a dark top. I found 0/30 same-runner results. #1 and #5 share a blue tie-dye pattern but are a different person. Most results show different readable bibs. Identity retrieval failed for this reference.
- 8039 (`…-040842.jpg`): #1 and #2 are the true runner from the same start-line burst. #3 (5180772, bib hidden) is plausibly the same runner: sage shirt, dark vest, headband, blue shorts. The rest are different people.
- 8010 (`…-040843.jpg`): #18 is the true runner, glasses and headband, bib read. #7 and #9 were counted as hits only because the 8010 runner is somewhere in the crowd. This led to the strict person-level metric (S2-T3 correction).
- Honest summary: fused mean over 36 bibs is photo R@50 0.354 and strict sR@50 0.341. Cross-photographer R@50 is only 0.153. Most successes are same-photographer bursts or start-line sequences. Recognising the same runner across photographers through clothing alone is weak, because the race has look-alike kit (many runners in black vests with headlamps at night). Clothing search is a candidate generator, not an identifier. Filters (time, photographer, bib) and labels (slice 3) have to carry the rest.

## S2-gate fix round 1 — search/eval (A, B, D, E)

**Decisions**
- A: `search.score` z-scores each term's per-person scores, `(s - mean) / std` over all loaded persons, before the weighted mean. It does this only when ≥2 terms are combined. A single-term query keeps raw cosine, so its printed score stays interpretable. A constant term (std 0) contributes 0. Multi-term printed scores are therefore weighted z-values (e.g. 3.1), not cosines.
- A: the z statistics use all persons, not just the filter mask. A filter then does not change a person's score, and the numbers stay comparable across filtered and unfiltered runs.
- A re-tune: the 36-bib eval under z-scored fusion still peaks at osnet 0.3 / siglip 0.7. The table is below; I picked by strict sR@50 with photo R@50 as tie-break. `WEIGHTS` is unchanged: osnet .3, siglip .7, text .5, scene .5.
- A, text/scene weight: kept at 0.5. With the photo terms summing to 1, 0.5 gives text/scene one third of the total weight after normalization. Demo on race925 stored vectors (no model load): for each of the 36 bibs' first ref, I added a pseudo-query and measured the top 24:
  - Pseudo-query was a random photo's scene vector, or a random person's siglip crop vector for `text`.

    | w | scene: overlap with photo-only | scene: share in term's top 20% | text: overlap | text: share in top 20% |
    |---|---|---|---|---|
    | 0 | 1.00 | .21 | 1.00 | .28 |
    | 0.1 | .85 | .27 | .89 | .28 |
    | 0.25 | .65 | .53 | .73 | .36 |
    | 0.5 | .46 | .74 | .49 | .51 |
    | 1.0 | .14 | .97 | .19 | .73 |

  - At 0.5 a combined query replaces about half of the photo-only results, and most results match the scene, so identity still leads. At 1.0 the scene dominates.
  - Caveat: the pseudo-queries are image vectors. A real text query has a different, noisier cosine distribution, and z-scoring removes only the scale gap, not the noise. The real `--photo … --scene 雪山` visual proof is left to the orchestrator (needs SigLIP).
- B: persons in photos with no scene vector get scene cosine −1 (the minimum possible), so they rank last on the scene term. It was previously 0, which was mid-pack. `search.check_scenes(db)` returns "embed_scenes incomplete: N of M photos have no scene vector yet; rerun `photofinder index`" (M = `status='ok'` photos). The CLI prints it after the OCR warning for any `--scene` query.
- D: `cmd_eval` runs `begin` right after `db.connect`, which has already committed its schema script. `load_persons`, the OCR check, every `ground_truth`, the sheets and `frequent_bibs` then read one deferred WAL snapshot. The connection is closed without committing; nothing is written.
- E: `--box` no longer defaults to 0; `query_box` uses `args.box or 0`. `--box N` or `--whole` without `--photo` exits with one line ("--box needs --photo" / "--whole needs --photo") before any model or index work.

**Rejected**
- Rank-based (percentile) normalization: it throws away how far ahead the top matches are, which is the signal that matters for top-k.
- Min-max scaling: one outlier person decides the scale.
- Normalizing single-term queries: printed scores would lose their cosine meaning, for no ranking change.
- Raising text/scene weights above 1 without normalization: the cosine ranges differ per query and per term (S2-T1 measured text↔image .01–.11 against image↔image .83–.88), so a fixed multiplier only fits the query it was tuned on.
- Excluding missing-scene persons outright: other terms may still rank them, and they already rank last on scene. The warning tells the user.

**Deferred**
- Correction to the S2-T3 Deferred line "Tuning text/scene weights (no labelled scene/text ground truth)". The scale problem is now fixed by z-scoring, and 0.5 is supported by the movement demo above. What remains deferred is a *labelled* tuning of text/scene weights, since there is still no ground truth for "right scene/outfit".
- z-scoring with a large share of −1 missing-scene rows (a very partial index) widens the scene std and slightly damps the scene term for present photos. It is acceptable while `embed_scenes` is incomplete, and the warning says so.

**Eval under z-scored fusion** (race925; R@10 / R@50 / xR@50 / sR@10 / sR@50; single-term rows unchanged by construction)
- Mean over 36 bibs (427 refs, 416 xrefs): osnet .214/.282/.090/.209/.268; siglip .200/.302/.102/.194/.285; **0.3:0.7 .253/.356/.155/.248/.342**; 0.5:0.5 .243/.339/.143/.237/.326; 0.7:0.3 .232/.313/.118/.227/.299.
- Fine sweep sR@50: 0.1:0.9 .327, 0.2:0.8 .341, **0.3:0.7 .342**, 0.4:0.6 .335, 0.5:0.5 .326.
- Bib 8038 (12 refs): osnet .144/.212/.081/.144/.212; siglip .091/.136/.019/.091/.136; 0.3:0.7 .167/.220/.067/.167/.220; 0.5:0.5 .174/.227/.081/.174/.227; 0.7:0.3 .152/.220/.081/.152/.220.
- Sheets: `data/exports/race925-eval-8038-20260928-045655.jpg`, `…-eval-8039-20260928-045656.jpg`, `…-eval-8010-20260928-045657.jpg`.

**Tests / mutations**
- `uv run pytest -q`: 154 passed. This includes the concurrent OCR doer's uncommitted changes, which I did not touch.
- New tests: combined terms z-scored (the scene term reorders a close osnet ranking where raw fusion would not); `check_scenes` count/None; missing scene ranks last (scene-only and combined); CLI partial-scene warning line; eval snapshot (a concurrent bib insert after `load_persons` is invisible); `--box`/`--whole` without `--photo`.
- Rewritten for z-scored fusion: the nearest-person, max-over-refs (now single-term) and renormalize (now 3 persons) tests.
- Mutations: 9/9 caught. They were: no z-score, z-score on a single term, z without std division, missing scene stays 0, warning never built, warning not printed, no read transaction, box/whole check removed, `--whole` ignored in the check. The dedicated z-score test alone also kills "no z-score". Restored from saved copies and sha256-verified.

**Touches**
- `src/photofinder/search.py` (`zscore`, `score`, `term_scores` scene sentinel, `check_scenes`) and `src/photofinder/cli.py` (search warnings, box/whole check, `--box` default None, eval `begin`).
- `tests/test_search.py`, plus this handoff.
- Did not touch `models.py`, `stages.py` or `test_ocr_bibs.py`.

### S2-T2 gate fix — Vision failure no longer reads as "no bib" (Codex MAJOR)
**Decisions**
- `models.read_text` now calls Vision via pyobjc directly instead of through ocrmac. It uses the same settings as the installed `ocrmac.text_from_image`: VNRecognizeTextRequest, level Accurate=0, languages ["en-US"], PNG bytes → VNImageRequestHandler.initWithData_options_, all inside `objc.autorelease_pool()`. It raises RuntimeError when `performRequests_error_` returns not-ok or a non-None error, and handles both the tuple and the bool return shapes. ocrmac returned [] in that case.
- `ocr_bibs`: if read_bibs/reader raises for one person, that person is not stamped. `ocr_at` stays NULL, so the next `index` run retries it. The stage increments a new `counts["ocr_errors"]` and logs one warning line: "ocr failed for person N in <relpath>, left pending: ...". The photo stays `ok` and the rest of the batch still commits. Only `except Exception` is caught, so KeyboardInterrupt still aborts and rolls back the batch.
- Equivalence evidence: I ran 70 real race925 crops (40 persons with a stored bib, 30 random at ≥200 px, with the stage's scaling applied).
  - New `read_text` matched ocrmac on 70/70 for raw (text, conf) and on 70/70 for tokens.
  - The tokens also matched the bibs stored by the full race925 run on 70/70. The race925 OCR data therefore stays valid.

**Rejected**
- Wrapping ocrmac: its `text_from_image` discards ok/err, so a failure cannot be distinguished from "no text".
- Marking the photo `error` on an OCR failure: the image decoded fine, and error photos drop out of search.

**Deferred**
- `ocrmac` stays in pyproject only as the provider of pyobjc-framework-Vision. Switching to a direct `pyobjc-framework-vision` dependency needs a pyproject/uv.lock edit.
- A persistent Vision failure leaves those persons pending on every run, logged once per person. It does not loop within a run.

**Touches**
- `src/photofinder/models.py`: `read_text`, plus `import io`.
- `src/photofinder/index/stages.py`: `ocr_bibs` per-person try/except, the `ocr_errors` count, and the log line.
- `tests/test_ocr_bibs.py`:
  - FakeReader gained an `error=` param, with KeyboardInterrupt as the crash default.
  - New test `test_ocr_failure_leaves_person_pending_and_photo_ok`.
  - `FakeVision` plus 5 read_text raise/success cases.
  - `ocr_errors` added to the counts asserts.
- The `ocr_bibs` counts dict gained the `ocr_errors` key.
- Mutations: 6/6 caught. They were: failure swallowed, err ignored when ok, failed person stamped, failed person marks the photo as error, ocr error not counted, and failure propagates. Files were restored and sha256-verified.

## S2-gate fix round 2 — invalid rows in term normalization

- Decision: `term_scores` marks invalid rows as NaN: a missing scene vector (it was −1 before) or a non-finite cosine from a NaN/inf stored embedding.
- Decision: `zscore` takes mean and std over finite rows only. `rank_invalid_last` then sets invalid rows to (min valid − 1), or −1 when no row is valid.
- Decision: a single-term query keeps raw cosine and also goes through `rank_invalid_last`. Invalid rows therefore rank last with a finite printed score; a missing scene now prints `min cos − 1` instead of −1.
- Effect: one NaN embedding no longer blanks its whole term. A mostly-unembedded scene index (the test uses 60% missing) no longer compresses real photos' scene z toward 0.
- Rejected: dropping invalid persons. The other terms may still rank them, and a missing scene is a partial-index state that `check_scenes` already warns about.
- Rejected: `-inf` as the fill value. It would print as `-inf` and turns weighted sums into NaN when the weight is 0.
- Tests: +2. `test_nan_embedding_ranks_last_and_does_not_blank_its_term` covers fused and single-term queries. `test_scene_term_still_moves_combined_ranking_on_mostly_missing_scenes` uses 6/10 photos without scene vectors. Both fail on ec3e818 (checked by swapping in the saved pre-fix `search.py`). The scene-only expected score for a missing photo is now `min − 1`.
- `uv run pytest -q`: 156 passed.
- Mutations: 6/6 caught. They were: stats over all rows, −1 sentinel restored, invalid filled with 0, zscore leaves NaN, single term not filled, single term z-scored. Restored and sha256-verified.
- Eval check: race925 has 0 photos without a scene vector and no NaN embeddings. 36-bib mean for 0.3:0.7 is unchanged at .253/.356/.155/.248/.342. The rerun's sheets went to scratch, not `data/exports`.
- Touches: `src/photofinder/search.py` (`term_scores`, `rank_invalid_last`, `zscore`, `score`) and `tests/test_search.py`.

## S2 whole-run gate (orchestrator)

- **Commits.**
  - Tasks: 4c0aa95 (T1, lean), 20bc34d (T2; risk: concurrency/locking, so it got a merged reviewer-verifier with 1 fix round, and the recheck closed 4/4), ab48ce2 (T3, lean, plus the orchestrator-requested strict-metric addition before commit).
  - Gate fixes: 4144297 and ec3e818 (round 1), 471427d (round 2).
- **Full suite.**
  - 142 passed at ab48ce2.
  - 154 after round 1.
  - 156 after round 2 (`uv run pytest -q`).
- **Whole-run reviewer.**
  - Verdict: APPROVE, with 1 MAJOR and 1 MINOR.
  - MAJOR: text/scene terms were inert when combined with `--photo` because of the cosine scale gap. Fixed in ec3e818 by per-term z-scoring for queries with 2 or more terms.
  - MINOR: partial scene index. Fixed in ec3e818 with a warning, and missing rows now rank last.
  - The round-1 re-check was APPROVE and raised a new MINOR: sentinels compressed the z-stats. Fixed in 471427d.
  - The round-2 re-check was APPROVE.
- **Codex** (3 findings).
  - Fixed in 4144297: ocrmac swallowed Vision failures, so a person was stamped as read with "no bib".
  - Fixed in ec3e818: eval read the DB in more than one snapshot.
  - Rejected: the HF offline check verifies "any complete snapshot" rather than the `refs/main` snapshot. The cache is only ever populated by hf_hub downloads, which write `refs/main`, and once the cache is complete offline mode stops any newer snapshot from being fetched. The failure needs a hand-prefetched cache.
  - The Codex re-check of round 1 found 1 MAJOR: a NaN embedding blanked a whole term under z-score. Fixed in 471427d, which also closed the reviewer's MINOR. Codex was not re-run after round 2, because that fix was small and targeted its own finding.
- **Real proof after the fixes.**
  - On scenemix, `--photo 71300735.jpg` alone and `--photo 71300735.jpg --scene 雪山` differ in 4 of their top-12 results. 3 of the 4 newcomers are in the scene-only 雪山 top 12, so the scene term now affects combined queries.
  - The race925 36-bib mean for 0.3:0.7 under the final scoring is R@10 .253, R@50 .356, xR@50 .155, sR@10 .248, sR@50 .342.
  - The OCR data is still valid after 4144297: the new `read_text` output is identical to ocrmac on 70/70 real crops.
- **Final defaults.** `WEIGHTS = osnet 0.3, siglip 0.7, text 0.5, scene 0.5`.
- **Deferred** (carried to slice 3 or later):
  - `bib_bonus` soft boost for the user's own known bib (slice 3 UI).
  - Crop tightening and ghost/prop handling. Cross-photographer identity is weak (xR@50 .155), and for bib 8038 the first reference returned 0/30 same-runner results.
  - Text/scene weights have not been tuned against labelled ground truth.
  - HF offline mode only engages after the first text query has fetched the tokenizer files, so machines that only run `index` still send HEAD requests.
  - Declare `pyobjc-framework-Vision` directly; it currently arrives through ocrmac.
  - A persistent Vision failure leaves persons pending forever, with a perpetual "ocr_bibs incomplete" warning.
  - An unreadable result photo causes a traceback at contact-sheet time, in both search and eval.
  - float16 in-memory scoring for about 350k persons.

- I re-checked the visual verdicts on the sheets rendered with the final z-scored default (`race925-eval-8038-20260928-045655.jpg` and `…-8039-…045656.jpg`), and they are unchanged. For bib 8038, 0 of the top 30 results are the same runner. For bib 8039, #1 and #2 are the true runner, and #3 (5180772, bib hidden) is plausibly the same runner.

implement-loop: slice 2 shipped 471427d; remaining: [3]

## Slice 3

Slice 3 SLICE_BASE=1c9b6a8

## S3-T1 — FastAPI backend (`photofinder serve`), float16 scoring, not_me negatives

**Decisions**
- API (`web/app.py`, `create_app(collection)`): `GET /api/facets`, `POST /api/upload`, `GET /api/uploads/{token}/image`, `POST /api/search`, `POST /api/labels {person_id, label: me|not_me|null}`, `GET /api/photos/{id}`, `GET /api/photos/{id}/image[?max=N]`, `GET /api/persons/{id}/crop`, `GET /api/me`, `POST /api/export`, `GET /` (placeholder `static/index.html`).
- Bib start lives in the one search endpoint: `start_bib` set → exact `b.text = ?` SQL path (not `score()`), best-conf person per photo, `order by taken_at is null, taken_at, id`, filters via new `search.filter_where`; `score` is null, `total` returned. Combining it with persons/upload/text/scene/more → 400.
- Negatives (`not_me` osnet vecs) apply in BOTH modes (spec formula is general); only `more` hides labelled items (me photos excluded, not_me persons masked as candidates). `score(..., negatives=)`: `pos − NEG_WEIGHT/Σw_pos · norm(max_cos(osnet, not_me))`, same norm as the positive part. `NEG_WEIGHT=0.3` is a constant, not a `WEIGHTS` key (eval passes custom weight dicts).
- float16: `matrix()` returns L2-normed float16 (scene zero row keeps the dtype); `max_cos` upcasts+renormalizes per `CHUNK=8192` (8192×768×4 = 25 MB temp, ×2 inside l2norm).
- Models: one `ThreadPoolExecutor(1)` for detect/embed/encode, awaited from `async` endpoints; scoring + SQLite run via `run_in_threadpool` so the loop stays free. `models.unload(*names)` (no args = all, unchanged for CLI); upload calls `unload("yolo")` after detect so OSNet/SigLIP stay resident.
- Upload embeds every box plus the whole image (last row) in one `embed_crops` call; stores re-encoded upright JPEG; `OrderedDict` of the last 8. Search never does model work for uploads.
- `parse_time` moved to `search.parse_time` (raises ValueError); CLI wrapper keeps `--from '…' is not a time; …` one-liner. Web → 400 `"start '…' is not a time; …"`.
- Errors: `RequestValidationError` → 400 one-line `{"detail": "field: msg"}`; `MissingEmbeddings` → 400. Missing/unreadable photo → 404 (full image pre-opens the file before `FileResponse`). `Cache-Control: private, max-age=86400` on all images.
- Export: `DATA_ROOT/exports/<collection.resolve().name>-YYYYMMDD.txt`, header `source_photo_id\tphoto_id\tpath`, path = `collection.resolve()/relpath` (the symlink inside the collection, not its target); same-day re-export overwrites.
- Deps: `fastapi 0.141.1, uvicorn 0.53.0, python-multipart 0.0.32` (+ starlette 1.6.0, pydantic 2.13.5, pydantic-core 2.46.5, annotated-types, typing-inspection). uv.lock: only additions, plus the `[options] exclude-newer` timestamp (global `exclude-newer = "7 days"`); no existing version moved.

**Rejected**
- Routing web search through `search.search()`: no candidate mask, no offset, one meta query per result.
- Separate bib endpoint: would duplicate filter parsing/validation.
- Targeted unload vs `unload()`: dropping SigLIP after each upload costs a ~13 s reload on the next text query.
- `WEIGHTS["neg"]`: leaks into eval configs/custom weight dicts.

**Assumptions**
- Candidate/labels read fresh per request (labels written by other tabs are seen immediately). Person-id validation uses embedded persons only; unembedded persons can be labelled but not used as refs.
- Upload cache is process memory, lost on restart (page must re-upload → 404 message says so).

**Deferred**
- bib_bonus: not implemented. The bib start already lists every OCR-matched photo, and once marked `me` they are hidden from find-more, so a boost would only reorder photos the user has already seen; under z-scored fusion a fixed +0.3 also has no stable meaning.
- `load_persons` peak at startup: fetchall of blobs + `b"".join` copies ≈ 3× the float16 matrix transiently (~2.7 GB at 350k). Steady state is fine; stream per-chunk if it bites.
- Contact-sheet traceback on an unreadable result photo remains in the CLI path (closed for the web path only).

**Real run** (race925, worktree code, port 8765): startup 8.7 s wall incl. `uv run`/imports (persons 0.48 s, scenes 0.08 s); RSS 295 MB start → 1.14 GB after SigLIP load → 820 MB after upload (MPS/compression) → 896 MB end. Facets 10 ms, 0 warnings. Bib start `8039`: 17 photos, 7 ms (#1 photo 1918 person 11785). Person-ref search 32 ms (scoring 26 ms). Labels 2 ms each. Find-more (2 me, 1 not_me) 33 ms. Text `red jacket` cold 14.6 s, warm 47 ms; text+scene warm 0.52 s. Upload 8015319.jpg (1920×8014, 11 boxes) cold 2.4 s (YOLO+OSNet load), warm 1.5 s; upload box 0 search 69 ms (#1 = photo 1918 itself). Photo detail 3 ms, full image 8 ms, thumb 480 → 480×320 12 ms, crop 76×256 8 ms, export 2 lines. Labels cleared (count 0), export file deleted, server stopped.
- Benchmark (synthetic 350k persons, 512+768 float16 = 896,000,000 B vs 1.79 GB float32): 2-term score+group 0.25–0.31 s; find-more 10 me + 10 not_me + drop 0.38–0.40 s; 3-term 0.38–0.47 s.
- Rankings: `eval --bib 8038 --bib 8039` identical to HEAD code (8039 run against `git archive HEAD` copy; 8038 matches the S2 table).

**Tests / mutations**: `uv run pytest -q` 193 passed (+35 `tests/test_web.py`, +2 `test_search.py`). Mutations 15/15 caught (bib substring, me-photo exclusion, not_me mask, refs=first me only, neg sign, neg not /Σw, neg raw under multi-term, chunk upcast/renorm, matrix float32, path not rooted at collection, label clear no-op, export '-' fallback, export relative path, whole not embedded, boxes unsorted); restored from copies, sha256-verified.

**Touches**
- New `src/photofinder/web/{__init__,app}.py`, `web/static/index.html` (placeholder for T2), `tests/test_web.py`.
- `search.py` (`parse_time`, `TIME_FORMATS`, `filter_where`, `NEG_WEIGHT`, `CHUNK`, `matrix`, `load_scenes`, `max_cos`, `score(negatives=)`), `cli.py` (`parse_time` wrapper, `serve`), `models.py` (`unload(*names)`), `tests/test_search.py`, `pyproject.toml`, `uv.lock`.
- Shared surface for T2: JSON shapes above; result keys `rank score person_id photo_id box source_photo_id taken_at photographer photographer_uid album width height relpath bibs label`.

## S3-T2 — static page `web/static/index.html` (vanilla JS, inline CSS)

**Decisions**
- One query model: a `base` start point (`{start_bib}` | `{persons:[id]}` | `{upload, box}` | `{mode:'more'}` | `{}` text-only) + current filters + description/scene, rebuilt on every run; Load more resends the same query with `offset = results.length`; done when `results.length >= total` (bib) or a page `< top`.
- Bib start sends only filters + `start_bib` (backend 400s on combos); "Search description" combines with a persons/upload/more base, runs alone after a bib base; summary line says when the description is ignored.
- Label state: one `S.labels[person_id]` map (server values overwrite on each fetch); after a successful POST, re-fetch `/api/facets` for header/Find-more counts and `/api/me` for the "My photos (N)" tab (photo count; `labels.me` counts persons) rather than counting locally. Failed POST → error banner, no visual flip.
- Box overlays use percentages of API `width/height` inside a shrink-wrapped `position:relative; display:inline-block` frame, so no onload/resize math. Modal image capped at `calc(100vw - 400px)` because `%` max-width on an inline-block child is cyclic.
- Photographer checkbox value = `uid || name` (filter_where matches either column).
- Time filters are text inputs (`YYYY-MM-DD HH:MM`), placeholders from facets `taken_at`; parse errors come back as 400 `detail` and show in the banner.
- Busy counter disables every `[data-run]` control during a request (backend runs one model job at a time); stale responses dropped via a sequence number. Text/scene/upload requests show the model-load time hint.
- Modal: side list of all persons (crop, bib reads with conf, Me / Not me / Find people like this); box click selects the list row; Esc / backdrop click closes; arrows step through the list the card came from (results or My photos).

**Rejected**
- `datetime-local` inputs: send `T` form that `parse_time` rejects, and drop seconds; text inputs match the CLI format exactly.
- Auto-running search on every filter checkbox change: many heavy requests; an explicit "Apply filters" (and Enter) instead.
- Popover per box: a side list is keyboard-reachable and shows bibs for every person at once.
- No backend change needed.

**Assumptions**
- DB `width/height` and boxes are in the EXIF-upright frame and the browser applies EXIF orientation to the full image (`image-orientation: from-image` default). Verified in code: `models.load_image` does `exif_transpose` and scan swaps width/height for rotated orientations.
- `/api/me` persons[0] is a fine card representative for photos with several me persons.

**Deferred**
- No browser run here (no browser available); visual/interaction quality is for the S3-T3 E2E screenshot pass.
- Unmarked photos stay visible in My photos until the view is reopened (lets the user undo).

**Touches**
- `src/photofinder/web/static/index.html` (full rewrite), `tests/test_web.py` (+`import re`, +`test_page_is_served_and_calls_only_real_endpoints`: every `/api/...` in the script == the app's `/api/` routes, both directions).
- Evidence: `uv run pytest -q` 194 passed; `node --check` on extracted script OK (node v24.14.0); mutations 3/3 caught (renamed facets path, renamed crop path, export call removed), restored + sha256-verified.
- Real server race925:8766: `/` 200 text/html; facets, start_bib 8039 (+ photographer uid + start filter → 8), photo 1918 detail, crop/thumb/full images, `/api/me`, find-more-without-labels 400 detail, bad time 400 detail, persons offset 60 → rank 61 all match the JS reads. No labels created (facets labels 0/0).

## S3-T3 — E2E fix round

**Decisions**
- Upload picker reuses the photo viewer (`openUpload`, `S.modal.upload`, box keys `u<index>`); auto-opens after an upload with ≥1 box; sidebar preview (click or "Choose person") reopens it and shows only the searched box. Per-person thumbs are client-side CSS crops (`cropThumb`: background-size/position, same 10% pad as `/api/persons/{id}/crop`) — no new endpoint.
- `S.start` (current combinable start point) is separate from `S.base` (last query). Set by `run()` for bib/persons/upload/more; chip "Combining with: … Clear" shows it (hidden for bib). Describe and Apply filters run `S.start` (or alone), so a cleared chip is not resurrected. Human labels (`base.label`) captured at click time, e.g. `the selected runner (22:27:32, 示例映画 摄影师丁)`.
- Non-bib start clears the bib input; `renderStart` re-renders the upload preview highlight. A new upload drops an upload start point (chip would otherwise name a box in the previous photo).
- Cards: crop and whole-photo thumb side by side (`.cropbox` + 42% thumb); rank and Me/Not me flag moved into the meta row so nothing overlays the person; score only in the rank `title`. Grid 208 px min, shot 226 px.
- Boxes: 1 px unlabeled outlines; tag only on hit/sel/hover (tag moved inside the box because `.frame` now clips); hit/sel 3 px `--pick` yellow; sel dims the rest of the photo via a 9999 px box-shadow (hence `overflow:hidden`). List-row hover highlights its box. Sticky list heading; `scrollIntoView({block:'center'})`.
- Viewer already had `role=dialog aria-modal aria-label` (T2); label now set per open.
- Banners cleared only when the view actually changes (run() calls showView on every search). Header timing = client `performance.now()` around the fetch, "took 66 ms" / "took 1.2 s".
- Counts updated locally from the `/api/labels` response: `S.facets.labels[prev]--/[new]++`, and a photo→me-person `S.mine` map (seeded from `/api/me` at start and on My photos) for the photo count; `refreshCounts` removed (orphan).
- My photos cards show `YYYY-MM-DD HH:MM:SS`; copy says Not me marks are not listed. From/To stacked. Inline empty favicon.

**Rejected**
- `/api/uploads/{token}/crop/{box}` endpoint: CSS crop needs no backend/test and the image is already cached by the browser.
- Picking on the 266 px sidebar preview (the T2 design): unusable with 11 overlapping boxes.
- Re-running `S.base` on Apply filters: would re-add a start point the user just cleared.
- Clearing banners inside every `showView`: would wipe warnings raised by the same run.
- Default-selecting box 0 in the picker: would dim the photo before the user chose anyone.

**Assumptions**
- Labels changed from another tab make local counts drift until reload (previous code refetched). Check: two tabs, mark in one.
- `/api/me` photos carry `photo_id` (photo_meta keys) — used to seed `S.mine`.

**Deferred**
- Duplicate YOLO detections (one runner boxed twice, box inside box, 07b) — index-stage issue (NMS/containment), not UI.
- Full E2E re-walk (labels, export, find-more, text search) left to the orchestrator; my headless pass (below) skipped label POSTs on purpose (race925 labels frozen).

**Touches**
- `src/photofinder/web/static/index.html` only. No backend/API change. Evidence: `uv run pytest -q` 194 passed (drift test unchanged and green); `node --check` OK (v24.14.0); mutations n/a (no backend logic).
- Headless Chrome (playwright, channel=chrome) vs own server race925:8771 (stopped after): bib 8039 → "took 35 ms"; viewer aria-label set, 16-person photo with only the matched box labelled, sticky heading, row centred; Find people like this → title "People like the selected runner (19:46:43, 示例影像)", bib input cleared, chip shown, Clear hides it; upload 8015319.jpg → picker auto-opens (11 boxes, Prev/Next hidden), box click selects, Search as me → "People like person 4 in your uploaded photo", preview shows only box 4, reopen keeps selection, Esc closes; 0 console errors. Found+fixed: CSS crop thumbs showed neighbours (contain-fit) → thumb sized to the crop aspect.
- Local counts with `/api/labels` intercepted by `page.route` (no DB write): 4/4/1 → Me A 5/5/1 → Me B same photo 6/5/1 → unmark A 5/5/1 → B Not me 4/4/2 → clear 4/4/1 (me persons / me photos / not_me).

### S3-T3 polish

- Viewer: matched/searched box = yellow `--pick`, user-selected = cyan `--sel` (+ shade); both → yellow. List rows get a "selected" tag (CSS-only, `.pp.sel .sel-tag`) next to "matched"/"searching now", and a Yellow/Blue legend line under the heading.
- Lists never auto-scroll on open (`scrollTop = 0`; the old `showSel` removed); only a box click scrolls, `block:'nearest'` with `.pp { scroll-margin-top: 48px }` so the row clears the sticky heading. Rejected: keeping the open-time centring with a margin fix: the brief preferred no scroll, and the matched box is already obvious in the image.
- Upload chooser: "Search as me" is secondary (`.srch`); only the selected row's button gets `primary` (toggled in `selectPerson`).
- Copy: Describe hint names the "Combining with" box; My photos sub-line says filters don't apply (chose the note over dimming the panel: simpler, no state); export banner adds "Replaces any earlier export from today."
- Evidence: pytest 194 passed, `node --check` OK; headless Chrome on own race925:8771 (stopped): list scrollTop 0 on open for viewer and chooser, 0 primary buttons before a pick, `['u9']` after; screenshots show yellow matched vs cyan selected; 0 page errors. No labels written.

## S3 orchestrator — real-browser E2E on race925

- Setup: `uv run photofinder serve …/data/subsets/race925 --port 8770` from the worktree; driven with Playwright MCP (Chromium, 1440×900 and 1100×800) by a separate driver agent. Three walks: round 1 (T2 page), round 2 (after the T3 fix round, fresh server, all labels cleared first), round 3 (targeted re-check after polish). Screenshots: `data/exports/screens/s3-e2e-*.png` (round 1), `s3-final-01…19-*.png` (rounds 2–3).
- Flow walked: bib 8039 start → mark 3 same-runner cards Me → Find more → 1 Not me + 1 Me → Find more again → full photo (single and 18-person) → Find people like this → upload `8015319.jpg` → Search as me → Clear start point → "red jacket" / "white cap" → photographer filter → My photos → Export → narrow viewport. 0 console errors in rounds 2–3 (round 1: favicon 404, fixed); 0 HTTP 5xx in either server log.
- Round 1 exposed 7 defects (19 s upload with "a few seconds" copy; unusable 266 px box picker; descriptions silently combined with the previous start point; tall crops as slivers with the inset thumbnail covering legs; overlapping box labels / weak highlight; stale banners, backend-only timings, internal ids in headers; truncated date inputs, favicon 404, facets refetch per label). All fixed in e3c67d3 and confirmed in round 2. Round 2 exposed 5 small ones (matched vs selected box identical; lists opened pre-scrolled; 11 primary buttons in the upload chooser; Describe copy after Clear; filter badge in My photos) — fixed in d9a1469 and confirmed in round 3.
- Search quality seen in the real flow (round 2, 3 clear single-runner Me marks from the bib-8039 list): Find more top 5 were all true bib-8039 photos; 7 of the 14 unmarked bib-8039 photos in the top 60. After 1 Not me (bib 8007) + 1 Me, every bib-8007 card disappeared and the top 6 stayed bib-8039; #10 was the same runner read as "bib 039" (19:59:43) — a photo the exact bib start cannot find. Upload "Search as me" on the same runner under stage lighting was weak (neon-yellow jackets; 6/17 bib-8039 photos in the top 60, best #16). "red jacket" alone → mostly red jackets/vests (incl. volunteers); "white cap" → near-zero precision. These are model limits already measured in S2 (xR@50 .155), not UI defects.
- Latency, warm, models loaded (curl, 3 runs, server-side wall): bib start 5 ms, person-ref search 29 ms, find-more 32–35 ms, text 33–35 ms, find-more + scene 47 ms, facets 9 ms, crop 14 ms, thumbnail 9 ms. Browser-measured in the walk: find-more 75–82 ms, person search 129 ms, label 3–47 ms, photographer filter 644 ms.
- Cold paths: first upload 19.2 s (round 1) / 21.5 s (round 2 fresh server) = YOLO + OSNet + SigLIP loads; later uploads 1.5–8.4 s (8.4 s when the process had been paged out). First text query 1.2 s when SigLIP was already loaded by an upload; 11.0 s in round 2 while the idle server had been paged out (see memory).
- Memory: server RSS 285 MB at start (footprint 212 MB); peak RSS 1.57–1.65 GB after models loaded; `footprint` 2.7–3.5 GB with OSNet + SigLIP resident. The machine was under outside memory pressure during the walks (other sessions/processes, ~65 MB free pages), and macOS compressed/paged the idle server down to 22–45 MB RSS, which is what made some warm requests take 0.6–1 s. Pressure level read 1 (normal) at the end.
- UI verdict (orchestrator, from the screenshots): calm dark UI, photos dominate, clear primary action; cards now show crop and whole photo side by side; viewer boxes land exactly on runners (single and 18-person photos), matched = yellow, selected = cyan; upload chooser is usable in a crowd; state feedback (counts, chips, banners, real timings) is consistent. Good enough to use; the limiting factor is appearance-model recall, not the page.
- Cleanup: the E2E labels (4 me / 1 not_me) and `data/exports/race925-20260928.txt` were deleted; race925 labels = 0. Servers stopped.
- Deferred from E2E: duplicate YOLO detections (box inside box, same runner) — index stage; uploads show no OCR bib reads; an uploaded photo that is itself indexed returns itself as #1 (bytes upload, no file identity); label changes in another tab drift the header counts until reload.

## S3-gate fix round 1 — paging, unembedded labels, label race, NaN negatives, ids, export, stale views

**Decisions**
- A: scored searches take `seen: list[photo_id]` as extra excludes; the page's Load more sends `offset 0 + seen = every photo in the grid` (bib start keeps `offset`, labels don't move it). Rank base = `offset + len(set(seen))`, so page 2 numbers from #61.
- B: only `q.persons` is validated against embedded persons (400 as before). me/not_me labels without embeddings are skipped for refs/negatives and reported as a warning ("N marked people have no embeddings yet; rerun `photofinder index`"; counts not_me always, me only in `more`); `more` with labels but zero embedded me → 400 "the people marked as me have no embeddings yet…". Me-photo exclusion and not_me masking still use all labels (the user marked them).
- C: `/api/labels` runs `begin immediate`, reads the old label, writes, and returns `previous`; the page adjusts counts from `r.previous`/`r.label` and ignores clicks for a person while its POST is in flight (`S.pending`).
- D: invalid (NaN-osnet) rows get the MAX valid normalized negative (max penalty): consistent with `rank_invalid_last` (an invalid row never beats a valid one); 0-fill would, in single-term raw-cosine mode, charge them less than any valid row.
- E: `loadMe` drops its response when `S.view !== 'me'`. J: summary reads filters from `S.query` and a `S.descIgnored` snapshot taken in `run()`.
- F: `Id = Annotated[int, Field(ge=0, le=2**63-1)]` on photo/crop path ids, `LabelBody.person_id`, `persons[]`, `seen[]` → 400 via the existing validation handler.
- G: export via `csv.writer(delimiter='\t', lineterminator='\n')` (minimal quoting; plain rows unchanged). H: `get_upload` = one `uploads.get()`.
- I (corrections to S3-T1, append-only): negatives apply in similar mode not "because the formula is general" but because a Not-me is never the user, so penalizing is always safe; `me` labels join refs only in `more` because similar means "people like THIS person". And the T1 "Rejected — targeted unload vs `unload()`" line reads inverted: the code DOES the targeted unload (`unload("yolo")`, keeping OSNet/SigLIP); the rejected option was unloading everything.

**Rejected**
- Page-side offset recomputation (count labelled-away cards): not_me marks in similar mode reorder the list, so no offset is correct; ids are.
- 0-fill for invalid negatives (see D). Request token for `loadMe`: view check covers the reported race; a double My-photos click re-renders identical data.
- Rejecting `seen` with `start_bib`: the page never sends it; ignored.

**Assumptions**
- `seen` lists stay small enough for one JSON body (≤ a few thousand ids at 60/page). Check: payload size after many Load mores.
- `begin immediate` serializes concurrent label writes across threadpool connections (WAL, timeout 30 s).

**Deferred**
- No concurrency test for the label transaction (only the `previous` field is tested); the in-flight guard + `previous` counting fix the reported double-click either way.

**Evidence**
- `uv run pytest -q`: 201 passed (+7 in `tests/test_web.py`; each failed on the pre-fix code: A/B/D/G assertion, C KeyError, F OverflowError, H KeyError). Mutations 8/8 caught (seen-exclude, unembedded filter, previous, neg fill, id bound, csv, rank base, get_upload check-then-read); restored from copies, sha256-verified.
- JS (A, C, E, J): headless Chrome (playwright, channel=chrome) against a fully intercepted fixture (`/`, all `/api/*` routed; no server, no DB) on the pre- and post-fix `index.html`. Pre → post: Load more body offset 60/seen 0 → offset 0/seen 60 (cards appended, card 61 = #61); double-click Me with held `/api/labels`: 2 requests, me 1→3 → 1 request, 1→2, clear → 1; unapplied filter: "filtered" shown → not shown; stale `/api/me` after switching back: heading "My photos (0)" → "More like my marked photos". 0 page errors. `node --check` OK.

**Touches**
- `src/photofinder/web/app.py` (SearchQuery.seen, Id, prepare/ranked signatures, labels response `previous`, export writer), `src/photofinder/search.py` (`score` negatives), `src/photofinder/web/static/index.html`, `tests/test_web.py`. API change: `/api/labels` response adds `previous`; `/api/search` accepts `seen`.
