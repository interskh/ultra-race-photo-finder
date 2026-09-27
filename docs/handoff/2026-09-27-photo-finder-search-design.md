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
