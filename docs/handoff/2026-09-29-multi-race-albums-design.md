# Handoff — multi-race albums (implement-loop run)

## Slice 1 · Task 1 — old yipai tag moves from photos.album to photos.grp; Group filter in backend

**Decisions**
- Migration: generalized `db.migrate` into `once(db, needed, sqls)` run twice (labels, then photos) as separate committed transactions; `MIGRATE` and `old_labels` unchanged so the labels-migration tests (which monkeypatch `db.MIGRATE`) still hold. Guard `old_photos` = photos exists and lacks `grp`; re-checked under `begin immediate`.
- `Filters.groups` placed after `bib` and passed by keyword at both call sites (web `filters_of`, cli `cmd_search`) so positional `bib` does not shift.
- `Result.grp` appended last; search meta select adds `grp` last (positional splat).
- Scan scope call: spec lists scan under Slice 2, but without moving the manifest tag to `grp` now, the next incremental scan on the new code would write tags back into `album` on already-migrated indexes. So `scan` writes the tag to `grp`; `album` stays null (Slice 2 fills album titles).
- `photo_meta` key is `grp` (matches column; originals CSV uses `r.get("grp")`). Photo API therefore returns `grp`; spec §4.5 names it `group` — task 2 may rename/alias for the UI.
- CSV: `group` column right after `album`; test fixtures in test_originals set `grp` (album null) to mirror a migrated legacy index.
- CLI result line prints group after album; `--album` help now says "source album".
- Test fixture `filter_index` keeps its album values and adds distinct grp values (1,3 = 终点; 2 = 起点; 4 = null) so album and group filters are tested independently.
- README: scan stage lists group; CSV columns list group.

**Rejected**
- Inserting `groups` before `bib` in Filters: silently shifts positional `bib` at both call sites.
- Replacing album with grp in `filter_index`: would drop album-filter coverage that Slice 2 still needs.
- Folding photos migration into the `MIGRATE` list: different guard; would break the labels rollback test that monkeypatches `MIGRATE`.

**Assumptions**
- Every legacy `album` value came from a yipai manifest tag (spec §3.5), so `grp = album, album = null` is exact. Checked on a scratch copy of `data/subsets/race925/index.sqlite`: 5745 × '9.25 赛事' moved to grp, album all null, reconnect unchanged.
- A pre-change server/indexer that opens a migrated index writes new scans' tags into `album` (old code). The running `index --ocr` on the 贡嘎 collection is old code and only runs OCR (no scan inserts after its start) — verify before restarting on new code.

**Deferred**
- UI Group dropdown / viewer group display (task 2).
- `photographer_uid` platform prefix, bare-uid matching, album titles/`album_key` population (Slice 2).

**Touches**
- `src/photofinder/db.py` (photos schema + migration — shared surface), `search.py` (Filters, Result), `index/stages.py` (scan), `web/app.py` (SearchQuery.groups, facets `groups`, photo meta `grp`), `cli.py` (`--group`), `originals.py` (CSV COLUMNS `group`), README.md.
- Tests: test_db, test_search, test_scan, test_web, test_originals.

## Slice 1 · Task 2 — UI Group filter; photo API/viewer show group

**Decisions**
- Photo detail API renames `grp` → `group` in the endpoint only (`meta["group"] = meta.pop("grp")`); `photo_meta` keeps `grp`, so originals/CSV (`r.get("grp")`) and other callers are untouched. Response carries `group` only, not both.
- UI: "Groups" checks list (`f-gr`, name `gr`, fed by `facets.groups`) placed right after Albums; wired into `filters()` (`groups`), active count, and the "filtered" sub-label. Clear already unchecks every checkbox in `#filter-form`, so no change there.
- Empty facets: reused the shared `checks()` helper, which shows "None in this collection" — same as Albums; Album list kept visible on legacy collections (no new hide logic).
- Viewer: `row('Group', d.group || '—')` after Album.

**Rejected**
- Renaming the key in `photo_meta`: would need changes in originals CSV and task-1 tests for no UI gain.
- Returning both `grp` and `group`: duplicate field in a public response.
- Hiding the Album dropdown when empty: new UI behaviour the spec doesn't ask for.

**Assumptions**
- Filters are not persisted (only profile id is in localStorage), so no persistence path to update — checked by grep.
- `/api/search` group filtering is already covered by task 1's parametrized `test_filters_restrict_search` groups cases; no new test added.

**Deferred**
- Rendered browser check of the Groups list/viewer row (no server allowed this slice); JS verified by `node --check` on the extracted script plus the existing page-endpoint test.

**Touches**
- `src/photofinder/web/app.py` (GET /api/photos/{id} response key `group` — public API), `src/photofinder/web/static/index.html`, `tests/test_web.py`.

## Slice 1 · Whole-run gate fix — `group` key consistent across web API

**Decisions**
- `photo_meta` now keys the `grp` column as `group`, so /api/search results, /api/me photos and /api/photos/{id} all emit `group`; the detail handler's pop is gone.
- `originals.write_csv` reads `r["group"]` (no `.get`): every row comes from `rows_of(photo_meta(...))`.
- Tests: new `test_search_results_carry_group`; /api/me test asserts `group` per photo.
- Docs: README filter step and ROADMAP filters list name group. Project CLAUDE.md test count not edited by the doer (agent-requested CLAUDE.md edits are out of bounds for it); the orchestrator/user should set it to 268 passing + 1 opt-in.

**Rejected**
- Keeping `grp` in photo_meta and renaming per endpoint: three places to keep in sync, which is how search/me were missed.

**Assumptions**
- No external client depends on the `grp` key (it existed only between task 1 and this fix).

**Deferred**
- Operational: the old-code `index --ocr` on the live 贡嘎 collection must finish before any new-code process opens that index. An old-code scan run after migration would write tags into `album` with `grp` null, and the migration (guarded on the `grp` column's absence) never re-runs to fix them.

**Touches**
- `src/photofinder/web/app.py` (photo_meta keys — public API for search/me/photo), `src/photofinder/originals.py` (write_csv), `tests/test_web.py`, README.md, docs/ROADMAP.md.

implement-loop: slice 1 shipped 5939e35; remaining: [2, 3, 4, 5, 6, 7]

## Slice 2 · Task 1 — race registry, slug resolution, race add

**Decisions**
- `races.py` reads `config.DATA_ROOT` inside every function (no module alias/default arg), so tests' monkeypatch redirects it.
- Missing `races.json` = empty registry; `save` writes `.races.json.<pid>.tmp` beside it then `os.replace`; tmp removed on failure. JSON is `ensure_ascii=False, indent=2`.
- One `RaceError(ValueError)` for bad/duplicate slug, empty name, unknown race, unsupported URL, missing id, duplicate album key; CLI turns it into `sys.exit(str(e))`.
- Slug and site_id checked with `fullmatch` (`$` alone accepts a trailing newline). site_id must be `[A-Za-z0-9_-]+` because the album key becomes a directory name.
- Host match is `host == d or host.endswith("." + d)` (rejects `notxxpie.com`). pailixiang site_id keeps the `a` prefix (§4.4); xxpie reads `album_id` from any path.
- `add_album(slug, url, title=None)`: title is an explicit optional arg, stored as-is; no network.
- `main()`: `require_mounted()` first, then resolve `collection` (only when the subcommand has one), then `setup_model_env()`. Existing directory wins; else a registered slug → `race_dir(slug)`; else exit "X is neither a directory nor a registered race (races: …)".
- Registered race whose dir doesn't exist → exit with a hint for every command, including `index` (no mkdir): `add_race` stays a pure JSON write; `race import`/`download` (Slices 3/4) create race dirs.
- Exports per race needed no code: `default_out` (`resolve().name`), `profile_folder` (`.name`) and `lock_index` already give `exports/<slug>/…` and `races/<slug>/index.lock`; covered by a CLI search-by-slug test.

**Rejected**
- `album add` CLI now: the spec puts it in Slice 4 with title fetch; a title-less variant would be a surface Slice 4 redefines.
- Creating `races/<slug>/` in `race add`: two writes (dir + JSON) and a half-state for no current user.
- Letting a registered slug beat an existing same-named directory: breaks the legacy "path always works" contract.

**Assumptions**
- `data/races.json` is gitignored via `data/` (checked: `git check-ignore -v data/races.json` → `.gitignore:3`).
- `config.require_mounted()` still checks the real DATA_ROOT (def-time default), as before this task; tests relied on that.
- Only the CLI edits the registry and never concurrently, so no file lock around read-modify-write (spec §3.1: "edited only by the CLI").

**Deferred**
- Registry lock for concurrent `race add`/`album add`: safe while a single user runs them by hand; revisit if Slice 4's `album add` runs from scripts.
- README/CLAUDE.md/ROADMAP mentions of `race add` and slugs: Slice 3 task 3 owns the docs update.

**Touches**
- New `src/photofinder/races.py` (registry API used by Slice 2 task 2 and Slice 3), `src/photofinder/cli.py` (`race add` subcommand, `resolve_collection`, `main()` order; `collection` help text), new `tests/test_races.py`. Shared surface: `data/races.json` format (§3.1).

## Slice 2 · Task 2 — yipai catalog view, multi-album scan, originals per album

**Decisions**
- `yipai.CATALOG_SELECT` is the single SQL for both the `catalog` view (in `SCHEMA`) and scan's read-only fallback (`select … from (CATALOG_SELECT)` when `sqlite_master` has no `catalog`); one `mode=ro` connection per manifest, closed before image reads.
- Scan uses the catalog for single-album collections too (keyed by text `source_id` = stem, no `int()`); album/album_key stay null and uids bare there.
- Race = collection has `albums/` dir. Album key = 2nd path part of `albums/<key>/…` (≥3 parts). Images elsewhere in a race dir (race root, `albums/x.jpg`) are skipped with a warning and not inserted (so not counted).
- Album title: registry lookup by key across all races (keys are globally unique); fallback to the key when unregistered or title is null, so the Album facet/filter still separates albums on a path-served race.
- Platform = registered album's platform, else key prefix before first `-`. uid prefixed only when not null (never `yipai:None`).
- Catalog `taken_at` used only when EXIF gives none; parsed strictly as `%Y-%m-%d %H:%M:%S`, `taken_ts` via the same naive-local `.timestamp()` as EXIF (shared `shot_time`); unparsable → both null.
- Photographer filter: third disjunct `substr(uid, instr(uid, ':') + 1) in (…)`; a no-colon legacy uid compares whole.
- `photo_meta` now emits `album_key` (needed by `rows_of`); `rows_of` groups metas by `album_key` → `albums/<key>/manifest.sqlite` for `yipai-*` keys, root manifest for null, nothing for other platforms (empty order_id/fname); lookup keyed `(album_key, photo_id)` so the same yipai id in two albums can't cross.
- `is_yipai`: root manifest or any `albums/yipai-*/manifest.sqlite`. Fetcher already uses `row["order_id"]` per row — unchanged.
- Facets need no change: photographers group by (uid, name), so a race lists prefixed uids; the UI sends `p.uid`, which hits the full-uid match.

**Rejected**
- Title null for unregistered albums: collapses every album into "no album" on a path-served race dir.
- Creating the view at scan time: legacy manifests (subsets, live yipai) are opened `mode=ro`; Slice 3 migration owns creating it.
- Keeping `load_manifest` for single-album and catalog only for races: two readers that can drift; the spec says scan reads only the catalog shape.
- Looking up album titles via the race whose `race_dir` equals the collection: fails for path-served copies/subsets of a registered race.

**Assumptions**
- yipai photo files are named `<photo_id>.jpg` with no leading zeros; the text-key lookup differs from the old `int(stem)` only for stems like `0101`. Checked: all 68,488 files in the live 贡嘎 `photos/` match `^[1-9][0-9]*\.jpg` (directory listing only).
- Only yipai manifests have `order_id`/`fname` in a `photos` table; `rows_of` selects by `yipai-` key prefix, not by registry platform.
- A new-code yipai `Downloader` opening an existing manifest will add the view via `SCHEMA` (harmless, no data change).

**Deferred**
- Originals status for non-yipai photos still reads "failed: not in the gallery manifest" (empty fname) — Slice 7 changes it to `open on site`.
- Catalog metadata changes after a photo is indexed aren't re-read (spec §7).
- `originals.file_name`/`statuses`/`read_csv` key on `source_photo_id` alone: two albums in one race sharing a source id would share an originals file name and CSV status. Safe for yipai-only races (site-global ids); Slice 7 (non-yipai originals/status) should key by album too.
- README/ROADMAP for race scan: Slice 3 task 3 owns docs.

**Touches**
- `src/photofinder/sources/yipai.py` (`CATALOG_SELECT`, `SCHEMA` view — shared manifest contract §4.2), `index/stages.py` (`load_catalog` replaces `load_manifest`, `ALBUMS`, `album_of`, `load_albums`, `shot_time`, `catalog_time`, scan), `search.py` (photographer filter), `originals.py` (`is_yipai`, `manifest_of`, `rows_of`), `web/app.py` (`photo_meta` adds `album_key` — public key in search/me/photo responses).
- Tests: new `tests/test_race_scan.py`; `test_scan.py` (legacy-no-view, catalog-table), `test_search.py` (bare uid).

## Slice 2 · Whole-run gate fix

**Decisions**
- Stray race images (race root, `albums/x.jpg`): counted up front as `counts["skipped"]` (key present for every collection, 0 outside races) and logged once per scan with the count and the first path; the per-file warning is gone. Still never inserted.
- Symlinked `albums/<key>` dirs: one warning per entry at scan time ("will not be scanned; use a real directory"); walk semantics unchanged (`os.walk`, no followlinks).
- `Registry.require(slug)` holds the single "no race … registered" lookup+error; `races.race` and `add_album` use it. `resolve_collection` loads the registry once.

**Rejected**
- Following dir symlinks in `find_images`: spec rejected dir symlinks (cycles, double-indexing a shared album).
- Tracking already-warned strays in the DB to warn only once ever: needs state; one summary line per run is enough.
- Codex MINOR on leading-zero stems (`0101.jpg` vs photo_id 101): the yipai downloader names files `f"{pid}.jpg"` from the integer photo_id (`sources/yipai.py:193`), so no yipai manifest produces such a stem.

**Deferred**
- A race mixing yipai and non-yipai albums makes `is_yipai` true for the whole race, so an originals job accepts non-yipai marked photos and records failures (empty fname) instead of "open on site". Impossible until Slice 4 adds non-yipai albums; Slice 7 owns that status.
- Album title is copied into `photos.album` at scan time, so a registry retitle leaves old rows stale (spec §7 metadata-refresh deferral); two albums with the same title merge in the Album facet (spec keys the filter on title).
- `album_key` is an additive field in photo API responses, not listed in spec §4.5.
- Project CLAUDE.md test count is stale; the orchestrator/Slice 3 docs task updates it.

**Touches**
- `src/photofinder/index/stages.py` (scan: `skipped` count — new key in scan's return dict and log line; symlink warning), `races.py` (`Registry.require`), `cli.py` (`resolve_collection`), `tests/test_race_scan.py`, `tests/test_scan.py` (count dicts gain `skipped`).

implement-loop: slice 2 shipped 64da651; remaining: [3, 4, 5, 6, 7]

## Slice 3 · Task 1 — race import

**Decisions**
- `config.DATA_ROOT` honours `PHOTOFINDER_DATA_ROOT`; `MODELS_DIR` pinned to the real `data/models`; `require_mounted()` checks `config.DATA_ROOT` at call time. Existing CLI tests that pointed DATA_ROOT at a nonexistent tmp dir now `mkdir` it (test_races fixture, 2 in test_search, 3 serve tests in test_web).
- All refusals happen in `plan()` before any lock or write: slug/name, yipai URL, orderId == dir name, manifest order ids ⊆ {orderId}, slug unregistered, key unowned, not both old+album dirs, not both indexes, not both export dirs, manifest present.
- Locks: `take_locks` opens each in `"a"` (never truncates serve.lock), flock LOCK_NB, on failure closes all taken and raises naming the holder. Race `index.lock` taken up front if the race dir exists, else right after `move_collection` creates it. fds held until after register.
- Registration is ONE `races.save` (Race with its Album), not `add_race` + `add_album`: two saves would leave a registered race without album on a crash between them, and the rerun would refuse forever. Validation reuses `races.SLUG`/`parse_url`/`owner`.
- Backup is taken on every run (resume too), from wherever the index is; name gets `-1`, `-2` on a same-second collision (a rerun must never overwrite the pristine first backup); written as `.part` then `os.replace`. Labels/profiles snapshot comes from the same connection; step 6 compares against this run's snapshot (no import step writes labels/profiles, so per-run comparison is sound).
- `move_index`: checkpoint(TRUNCATE) + close, then refuse if `-wal`/`-shm` still exist (measured: last close deletes both; a checkpoint with another reader open still reports busy=0, so leftover files are the reliable "someone else has it open" signal).
- `--title` defaults to the race name; the same string goes to `photos.album` and the registry, so scan's `title or key` yields it.
- CSV fix: whole-text replace of `<old dir>/` and `<old exports>/` (abspath and resolved forms) — the live CSVs are the pre-`group` 8-column format, so no column parsing. BOM kept as found; atomic via `yipai.write_atomic`.
- Symlinks (SPEC DEVIATION from §3.8 "fullcopy only"): every symlink among each `subsets/*` top-level entries and its real `photos/` dir entries whose `readlink` equals or starts with `<old dir>/` is re-pointed (tmp symlink + `os.replace`). Live has ~8.5k per-file links in race925/first2000/scenemix plus fullcopy's dir link; without this acceptance item 2 breaks.
- SIGKILL test uses a test-only env hook `PHOTOFINDER_RACE_IMPORT_KILL_AFTER=<step>` (kills itself right after that step) — deterministic, runs the real `python -m photofinder.cli race import`.
- Progress lines `done: <step>` are printed (flushed) after each step.

**Rejected**
- FIFO-blocking subprocess for the kill test: no production hook, but needs polling/sync on a path that moves mid-import.
- Moving leftover `-wal`/`-shm` with the index: only exist when another process has it open; refusing is safer.
- Persisting the step-2 snapshot to disk for resumes: labels are never written by the import, so a per-run snapshot detects anything this code could break.

**Assumptions**
- The rerun passes the same collection path (old-dir prefix is derived from it; the dir is gone on resume). Check: summary line prints the path.
- Live symlinks and CSV paths are absolute (checked: `readlink` of race925/fullcopy entries, Me/FriendA CSV heads). Relative links would not be re-pointed.
- The live index already has `profiles` (post people-migration); a pre-profiles index would fail the snapshot query.

**Deferred**
- Relative symlinks / CSVs written with a relative collection path: none exist live.
- Rehearsal on a copy of the live index and the live run: Slice 3 task 3.

**Touches**
- New `src/photofinder/race_import.py`, `tests/test_race_import.py`; `cli.py` (`race import` subparser, `cmd_race_import`); `config.py` (env override, `require_mounted` — shared); tests: test_races, test_search, test_web (data root mkdir only).
- Shared surfaces: `data/races.json` (one atomic save), `data/backups/`, `data/exports/<slug>/`, `data/subsets/*` symlinks.

**Task 1 fix round**
- Race `index.lock` is now taken before any move: after the collection/serve locks, `run` creates `races/<slug>/` (if absent) and locks its `index.lock`, then backs up and renames. Before, an indexer starting in the mkdir→rename window could create `races/<slug>/index.sqlite` and wedge every rerun as "ambiguous". Test `test_indexer_cannot_start_on_the_race_during_the_move` fails on the old ordering (checked by mutant).
- `plan()` refuses when the collection dir or the resume album dir is a symlink (scan skips symlinked album dirs; a renamed link would register an unscannable album). Test `test_refuses_a_symlinked_collection`.
- Deferred (parked by the orchestrator): `serve.lock` follows `PHOTOFINDER_DATA_ROOT`, so a tmp-root rehearsal does not see the live server's lock (Task 3 documents it); raw OperationalError/EXDEV tracebacks instead of friendly messages; one new backup per retry (disk use on repeated reruns). Caveat: do not run the import while an indexer runs on a `data/subsets/*` collection — its links are re-pointed underneath it and no subset lock is taken.
- Follow-up: `backup()` first deletes this collection's leftover `<name>-index-*.sqlite.part` / `*.sqlite.part-journal` (from a backup killed mid-copy, up to ~800 MB each); finished `.sqlite` backups and other collections' files are never touched (`test_leftover_partial_backups_are_removed`).

## Slice 3 · Task 2 — download CLI and scripts

**Decisions**
- `photofinder download <race> [album_key]`: `cli.select_albums` validates slug and key before any filesystem write (unknown slug lists races, unknown key lists the race's keys, a race with no albums exits). Same function is the pre-flight check in `download.sh`.
- Failure policy: `Blocked` (exit 2, logged "rerun later to resume") and `AlreadyRunning` (exit 1 with the lock message) stop the whole run: every yipai album is the same host, so continuing after a breaker trip or beside a live download would be the hammering the project rule forbids. An album that ends with non-`done` counts (failed/pending/expired/missing) lets the loop continue; the run exits 1 at the end naming those albums.
- Non-yipai albums print the skip line and do NOT count toward the nonzero exit (Slice 4 makes them real; today no CLI can register one).
- Seams `cli.yipai_client()` / `cli.yipai_downloader()` hold the production settings (`httpx.Client(timeout=60, follow_redirects=True)`, `concurrency=6, page_delay=3.0`); tests patch them. A test pins those kwargs.
- Per-album `FileHandler(<album_dir>/download.log)` on the root logger, removed and closed in `finally` (also on the sys.exit paths). httpx logger set to WARNING as yipai.main does.
- Added `Downloader.close()` (db + lock file) so sequential albums don't leak an open sqlite/lock per album; called in `finally`.
- `download.sh` validates the race (and key) with `uv run --frozen python -c … cli.select_albums` BEFORE `mkdir -p data/races/<race>`, so a typo'd slug leaves no dir. Console log: `<root>/races/<race>/download-console.log`.
- `yipai.refusal(order_id)` (registry owner of `yipai-<id>`) is shared by `download_yipai.sh` (python -c before `mkdir`) and `yipai.main` (before `out_dir.mkdir`, only when `--data-root` resolves to `config.DATA_ROOT`).
- `yipai.main --data-root` now defaults to `config.DATA_ROOT` (honours `PHOTOFINDER_DATA_ROOT`); the duplicate `yipai.DEFAULT_DATA_ROOT` is gone. Both scripts use `${PHOTOFINDER_DATA_ROOT:-data}` and `download_yipai.sh` passes `--data-root "$root"` so console.log, photos and registry share one root.
- Shell tests put a fake `caffeinate` first on PATH, so even a broken validation (or a mutant) never starts a real background download. `import photofinder.cli` (the pre-flight) imports no torch/open_clip/ultralytics (checked with `-X importtime`).

**Rejected**
- Continuing to the next album after `Blocked`/`AlreadyRunning`: doubles load on a site that is refusing us or already being downloaded.
- Letting the CLI create the race dir and have the script redirect elsewhere: the `>>` redirect needs the dir before `nohup`, so the shell must validate first.
- Monkeypatching `time.sleep` in tests: `Downloader`'s `sleep=time.sleep` default is bound at class definition.
- Refusing in `yipai.main` for any `--data-root`: a custom root is an explicit experiment location, not the registry's collection.

**Assumptions**
- Run from the main checkout, repo-relative `data` == `DEFAULT_DATA_ROOT`. From a worktree without `PHOTOFINDER_DATA_ROOT`, the scripts' `data/` is the worktree's while python reads the live registry (pre-existing mismatch; set the env var in worktrees).
- Anyone who had `PHOTOFINDER_DATA_ROOT` set now gets yipai downloads under it (previously always the live root).
- The two subprocess script tests need `/Volumes/Ext1TB` to exist (the scripts' mount check runs first); true on this Mac only.

**Deferred**
- No race-level lock: two `download` runs of the same race serialize per album via `.download.lock` (the second exits on the first busy album).
- README/ROADMAP/CLAUDE.md mention of `download`/`download.sh`: Slice 3 task 3 owns docs.

**Touches**
- `src/photofinder/cli.py` (`download` subcommand, `select_albums`, `yipai_client`, `yipai_downloader`, `download_yipai`, `cmd_download`; imports httpx + sources.yipai), `src/photofinder/sources/yipai.py` (`refusal`, `Downloader.close`, `--data-root` default, imports config/races), `scripts/download.sh` (new), `scripts/download_yipai.sh`, `tests/test_download.py` (new).

## Slice 3 · Task 3 — rehearsal on a copy, docs

**Rehearsal** (real CLI `uv run --frozen photofinder race import …` from the worktree, `PHOTOFINDER_DATA_ROOT=/Volumes/Ext1TB/Projects/photo-finder-scratch/2026-09-29-import-rehearsal2/data`; collection copy = SQLite-backup-API copies of the live index.sqlite (taken while the old-code OCR run was writing; old schema, no grp) and manifest.sqlite + a `photos` symlink to the live photos dir; exports/<id> copied with CSV paths re-rooted to the scratch root; subsets: fullcopy/photos dir symlink, 3 per-file symlinks, an unrelated link and a look-alike `…/<id>0/…` link)
- pre: 68,488 photos, 190,980 persons, 3 profiles, 69 labels (sha256 of labels+profiles e8872f8c87ba1801).
- Refused (exit 1, nothing changed) while the copy's index.lock was held, and while serve.lock was held.
- Run 1 SIGKILLed during the backup (378 MB .part left); run 2 SIGKILLed by the test hook right after move_index (index already in races/2026-gongga100/, relpaths not yet rewritten); run 3 same command completed in 2.3 s, removed the stale .part; run 4 refused ("race 2026-gongga100 is already registered").
- post: labels/profiles hash identical (e8872f8c87ba1801); 68,488 relpaths == {'albums/yipai-83415673067642538672/photos/' + f for f in live photos listing} and all resolve; all 68,488 uids `yipai:`-prefixed, 0 double; album 'FUGA 贡嘎100' / key yipai-83415673067642538672 on all rows; grp from the migration: 9.26 赛事 52,175 · 9.25 赛事 5,752 · 9.25 签到 5,371 · 定妆照 4,486 · 9.27 赛事 444 · 挑选图片 189 · null 71; catalog view 68,488 rows all done; 3 photos.csv rewritten (BOM kept, every path now under races/…/albums/… and exports/2026-gongga100/); 4 subset symlinks re-pointed, the unrelated and look-alike links untouched; registry holds the race with title "FUGA 贡嘎100".
- `index 2026-gongga100 adds 0 photos` could not be shown on the copy (photos is a dir symlink; os.walk doesn't follow it) — the relpath-set equality above is the equivalent check; the real scan is verified on the live run.
- Live index.lock probe (read-only flock attempt, released at once): HELD at 10:07 during the old-code OCR run; that run finished at 10:16:37 ("index finished in 7729.9s"), and a second probe found it FREE. The live collection has NOT been imported.
- A first rehearsal (scratch root …-import-rehearsal) showed the stale-.part leak (fixed in 7bea619) and that CSVs pointing at another root are left alone.

**Decisions**
- README: new "Races" section (race add, race import, layout, slug-or-path) before the numbered steps; step 1 is `scripts/download.sh <race>`, `download_yipai.sh` kept as the path for unregistered galleries; new "another data root" section for `PHOTOFINDER_DATA_ROOT`.
- README says outright that albums can't be added to a race from the CLI yet (no `album add` until Slice 4): `race add` alone yields a race `download` rejects ("has no albums yet").
- CLAUDE.md test count 370 passed + 1 opt-in (measured: `uv run pytest -q` → 370 passed, 1 skipped = `test_model_memory.py:269`, PHOTOFINDER_REAL_MODELS).
- ROADMAP: the Done line and the pending-migration line are added beside master's untouched lines; the old Top-up line is kept as-is (still true until the import).

**Rejected**
- Rewriting ROADMAP's FUGA/OCR and Top-up lines: master 72e538c (not on this branch) rewrote the neighbouring OCR lines; editing them here would conflict at merge and duplicate its facts.
- Fixing README's "one lookup per second" for originals (code: `originals.LOOKUP_GAP = 6.0`): pre-existing, outside this task.

**Assumptions**
- `data/exports/2026-gongga100/{Me,FriendA,FriendB}` in step 3 below comes from the rehearsal's copied exports; check with `ls data/exports/83415673067642538672` before the live run.

**Deferred — HUMAN-ACTION STEP (live import)** — run from the main checkout /Volumes/Ext1TB/Projects/photo-finder after merging the branch; the user must confirm touching production data:
1. Stop-check: `pgrep -fl photofinder` prints nothing (no index, serve, download, or subset indexer); the import also refuses by flock if any of the collection's index.lock/.download.lock, data/serve.lock is held.
2. Do NOT run `race add 2026-gongga100` first (import creates the race). Run: `uv run photofinder race import 2026-gongga100 "2026 贡嘎100" data/yipai/83415673067642538672 --url "https://www.yipai360.com/photolivepc/?orderId=83415673067642538672" --title "FUGA 贡嘎100"`. If interrupted, rerun the identical command (forward recovery).
3. Verify: the printed summary shows persons 190,980, profiles 3, labels 69 "(unchanged)" and photos 68,488; `data/yipai/` no longer has the id; `data/races/2026-gongga100/index.sqlite` exists; `data/exports/2026-gongga100/{Me,FriendA,FriendB}`; `ls -L data/subsets/race925/photos | head` resolves; backup (~835 MB) in data/backups/.
4. Optional: `uv run photofinder index 2026-gongga100` should scan 0 new photos (it is one heavy job; later stages have nothing pending apart from OCR, which only runs with --ocr).
5. Restart the server only if the user wants it running: `uv run photofinder serve 2026-gongga100`.
6. Top-ups from now on: `scripts/download.sh 2026-gongga100` (download_yipai.sh refuses this order id once it's registered).
7. Delete the scratch rehearsal roots /Volumes/Ext1TB/Projects/photo-finder-scratch/2026-09-29-import-rehearsal{,2} (~2 GB) once satisfied.
- Memory note (orchestrator/user, not the doer): fuga-download-topup.md names `scripts/download_yipai.sh 83415673067642538672` and data/yipai/<orderId>/ — after the live import it should say `scripts/download.sh 2026-gongga100` and data/races/2026-gongga100/.
- After the live import: CLAUDE.md's "贡嘎 stays here until the live `race import` runs" and ROADMAP's pending-migration/Top-up lines need a one-line update.

**Touches**
- README.md, CLAUDE.md (project), docs/ROADMAP.md (merge with master 72e538c: both set "Last updated 2026-09-29"; no other overlapping lines).

## Slice 3 · Whole-run gate fix

**Decisions**
- `yipai.refusal` also refuses when any `DATA_ROOT/races/*/albums/yipai-<id>` exists (import in progress or unfinished → "rerun `photofinder race import <slug> …`"), so `download_yipai.sh` / `python -m photofinder.sources.yipai` can't recreate `data/yipai/<id>/` and wedge the import as "both exist". Registered owner still wins (its message first).
- Kill hook prints `PHOTOFINDER_RACE_IMPORT_KILL_AFTER=<step> is set (test hook): killing this import after <step>` to stderr before SIGKILL; the SIGKILL test asserts it.
- `verify` accepts exactly one profiles difference: backup had no profiles and the race index has only `db.DEFAULT_PROFILE` (inserted by `db.connect`) with no labels on it. Any other profile/label change still refuses (renamed, extra profile, label added — tested).
- Mutants: default never accepted / always accepted / refusal glob removed / hook notice removed → 4/4 caught.

**Corrections to the Task 3 human-action list** (replace those lines):
- Step 1 adds: start nothing (no download, index, serve) until the import prints its summary.
- Step 3: the labels/profiles check is the summary's "(unchanged)" — 69 labels / 3 profiles were the copy's counts, not a hard expectation (the user may have added marks since). The backup is about the index size (~0.8 GB).
- Step 4 is REQUIRED, not optional: `uv run photofinder index 2026-gongga100` must scan 0 new photos — the only live check that relpaths match the scan (the rehearsal couldn't show it).
- CLAUDE.md not edited here (doer does not edit CLAUDE.md on agent request). Needed there: test count 376 passed + 1 opt-in, and line 24's `race import` rule could add "start nothing until the summary; then `index <slug>` must scan 0 new".

**Deferred**
- Registry read-modify-write is unlocked vs a concurrent `race add`: single user by hand (already deferred in Slice 2).
- `scripts/download.sh` creating a missing race dir: rejected as a fix — new races legitimately have none until their first download.
- download.sh's console-log path is checkout-relative without `PHOTOFINDER_DATA_ROOT`: documented; scripts run from the main checkout.
- download and index on the same race share no lock: pre-existing rule (one heavy job at a time, by hand).

**Touches**
- `src/photofinder/sources/yipai.py` (`refusal`), `src/photofinder/race_import.py` (`default_only`, `verify`, `checkpoint`), `tests/test_download.py`, `tests/test_race_import.py`, README.md, docs/ROADMAP.md.

implement-loop: slice 3 shipped 7f97594; remaining: [4, 5, 6, 7]

## Slice 4 · Task 1 — sources/common.py and AlbumDownloader base

**Decisions**
- Adapter contract (duck-typed, used by task 2 and slice 5): `meta() -> {"title": str, "total": int | None}`; `list_page(cursor) -> (rows: list[CatalogRow], next_cursor | None, total | None)` — cursor is opaque (None = first page; the base only passes back what the adapter returned; calling again with the same cursor must re-list that page with fresh signed URLs); `preview_url(row) -> str` (absolute, fetchable); optional `meta_items() -> dict` written to `meta` after `title`.
- `CatalogRow(source_id, fname, photographer_uid, photographer, group_name, taken_at, width, height, url)`; `url` is the listing's preview URL, non-persisted (`compare=False`). The base calls `preview_url(row)` on the row from the current listing, so the 403 re-list always uses the fresh one.
- `AlbumDownloader(client, adapter, out_dir, *, concurrency=4, page_delay=3.0, img_delay=(0.2, 0.6), tries=5, max_consecutive_failures=20, sleep=time.sleep, clock=time.monotonic)`; `acquire_lock()`, `close()`, `run() -> {status: count[, "missing": n]}`.
- Persisted statuses are only pending|done|failed (§4.2). A 403 persists as `failed`/`error='HTTP 403'` (`base.EXPIRED`); the re-list decision reads that error in memory. yipai's `expired` status is not copied.
- `total`: meta's total, overridden by any non-None total from `list_page`; None all run → no warning, no `missing` key (xxpie).
- Resume check `jpeg_file_ok(path)`: same rule as `looks_like_jpeg` (size > 1024, `FFD8` head, `FFD9` in last 64 bytes) but reads only 2 + 64 bytes via seek; a full read per done file would be tens of GB per top-up of the 93k photoplus album. `download()` still marks a valid existing file done without fetching (crash between write and commit).
- Listing helper: new `common.fetch_json(client, method, url, *, check=identity, tries, sleep, **kw)`; `check(body)` returns data or raises ValueError (retried like bad JSON); non-retryable HTTP → `Blocked`; exhausted → `RetriesExhausted`. `yipai.request_json` left untouched (originals.py pins its behaviour and `HEADERS`).
- `preview_url(row)` runs in the worker threads (inside `download()`), so it must be pure/thread-safe.
- No headers in the contract: the caller configures `httpx.Client(headers=…)` (task 2 seam like `cli.yipai_client()`).
- Logger name `download`; per-page line `page N: fetched X, done D/T, M min elapsed` (T is `?` when total unknown). Elapsed uses the injected clock.
- Upsert updates metadata columns only; status/file/error untouched (tested directly on `upsert`, since `download()`'s file check masks a reset in end-to-end runs).

**Rejected**
- Adapter `pages()` iterator (spec wording): cannot re-yield a page for the 403 re-list.
- Integer page cursor: photoplus (slice 5) needs sub-album + page + reconcile phase in the cursor.
- Caching preview URLs by source_id in the adapter: a stale cache would silently defeat the re-list.
- Generalising `yipai.request_json` with a callback: risk to originals' pinned behaviour for no gain.

**Assumptions**
- Adapter source_ids are filename-safe (hex/numeric per §Facts); no path sanitising in the base. Check in each adapter.
- flock conflicts between two fds in one process on macOS (the lock test relies on it; passes).

**Deferred**
- CLI routing and per-album `download.log` handler: task 2.
- OSError-on-write and retryable cool-down paths are copied from yipai and not separately tested in the base (covered in test_yipai for the yipai copy).
- Project CLAUDE.md test count is stale: `uv run pytest -q` → 391 passed, 1 skipped (opt-in); not edited by the doer.

**Touches**
- New `src/photofinder/sources/common.py`, `src/photofinder/sources/base.py` (catalog/meta schema — shared read contract with `index/stages.load_catalog`), `tests/test_base_downloader.py`; `src/photofinder/sources/yipai.py` (helpers now imported from common; same objects).

**Task 1 fix round**
- A 403 on a page's first listing neither counts toward nor resets the breaker (`download(row, relisted=False)`); the re-listed attempt counts every failure, a second 403 included. Before, a page of ≥20 expired URLs tripped the breaker and skipped the re-list. This deliberately differs from yipai's `Downloader`, which still counts first-listing 403s.
- If `next_cursor == cursor`, the loop logs a warning ("adapter returned the same cursor … ending the listing") and ends the listing. It does not raise Blocked because this is an adapter bug, not the site refusing us, and Blocked's "rerun later" would just repeat it. The run still returns counts, and `missing` shows the gap when the total is known.
- Tests: whole page of 30 expired URLs → all done, no Blocked; 403 on both listings → Blocked after the re-list (20 failed, 10 pending); an adapter that repeats its cursor → 2 listings, then a warning. All three failed on the pre-fix code. Mutants 8/8 caught (the original 5 plus: first-listing 403 counts; re-listed 403 not counted; no cursor guard).
- Parked: the schema is created in `__init__`, before `acquire_lock`. If two processes start within the sqlite timeout, the second can get "database is locked" instead of AlreadyRunning. Deferred, same as yipai. `fetch_json` stays because task 2's pailixiang adapter uses it.

## Slice 4 · Task 2 — pailixiang adapter, album add, download routing

**Decisions**
- SPEC/TASK DEVIATION: `meta()["total"]` is always None. `Data.PhotoSearchCount` in the real AlbumGetView is 80, the page size (beside VideoSearchCount 20 and CommentSearchCount 100), not the album total. The total comes from each listing's `TotalCount` (715 live).
- `common.fetch_json` gained `fresh=dict`: a callable whose kwargs are rebuilt on every attempt. The adapter passes `json=` through it, so each retry (Code≠0 included) sends a new `ak`. Existing callers are unchanged.
- Cursor = StartIndex int (None → 1); next = start+80 while `len(Data) >= 80`, else None. `opt_time` is stored on the adapter from the first listing response, so later pages and a re-list of page 1 both echo it.
- `Code != 0` raises ValueError inside `check`, so it is retried like bad JSON and ends in RetriesExhausted. Rows whose `ID` isn't `[A-Za-z0-9_-]+` are skipped with a warning (the ID becomes a file name).
- `meta_items()` key is `album_id`, the same as task 1's FakeAdapter.
- `races.check_album(reg, slug, url, title)` does the validation with no save; `add_album` now calls it. `album add` runs it before any client exists, fetches the title, then calls `add_album` (which re-validates).
- Title fetch failure: catches `Blocked` (RetriesExhausted included) plus KeyError/TypeError for a malformed body → exit suggesting `--title`. Nothing is registered. An empty fetched title is stored as null.
- CLI seams: `ADAPTERS = {"pailixiang": module}` (module provides `HEADERS`, `Adapter`); `album_client(platform)` (headers, timeout 60, redirects); `album_adapter(platform, client, site_id)`; `album_downloader(client, adapter, out_dir)` passes the A6 values explicitly (pinned by a test). `download_yipai` became `download_album`, one log/close/exit wrapper for both kinds.
- Blocked on any album stops the whole run (exit 2), even albums on another host. A breaker trip means the pacing assumptions are already wrong. Continuing elsewhere with the same settings is the same gamble, and the user reruns anyway. Tested with pailixiang blocked and the yipai album never started.
- Fixtures copied verbatim to `tests/fixtures/pailixiang_{list,view}.json`. Multi-page tests clone the first real row with synthetic IDs.

**Rejected**
- A retry loop inside the adapter (fetch_json with tries=1): it would duplicate the backoff/Retry-After/Blocked logic.
- Validating in the CLI by copying the parse/require/owner code: this would give two copies of the duplicate-key rule.
- Using PhotoSearchCount as the total: it is wrong (see above), and it is only harmless because TotalCount overrides it.
- Continuing to other-host albums after Blocked: see Decisions.

**Assumptions**
- Reusing an OptTime across pages is fine for a re-list after a 403. The probe only showed page 2 and the 641 page with the first OptTime. Check on the real download's re-list log line, if one ever occurs.
- `ShootTime` is camera-local Beijing time (spec A4/§Facts), so it is stored as-is.
- Previews carry no EXIF (spec), so `taken_at` always comes from the catalog. The scan test uses an EXIF-less JPEG.
- `album add` title fetch on a connection error retries 5× with backoff (~30 s worst case) before the `--title` hint; a 403 exits at once.

**Deferred**
- Real pailixiang download/index (715 photos): the orchestrator runs it. No live request was made here.
- `site_link` for pailixiang: Slice 7.
- CLAUDE.md test count (now 420 passed + 1 skipped): not edited by the doer.

**Touches**
- New `src/photofinder/sources/pailixiang.py`, `tests/test_pailixiang.py`, `tests/fixtures/pailixiang_{list,view}.json`.
- `src/photofinder/sources/common.py` (`fetch_json(fresh=)` — shared helper), `races.py` (`check_album` — registry API), `cli.py` (`album add` subcommand, `ADAPTERS`, `album_client`/`album_adapter`/`album_downloader` seams, `download_yipai` → `download_album`, routing), `tests/test_download.py` (skip test now uses xxpie; pailixiang CLI download, pacing, headers, blocked tests), README.md, docs/ROADMAP.md.

**Task 2 fix — TotalCount 0 on later pages**
- Found in the live run: after page 1, AlbumSearchPhoto answers `TotalCount 0` when OptTime is echoed (`done 160/0`). Because base lets any non-None page total override, the listed-vs-total check compared against 0 and could never report a shortfall.
- Fix is in the adapter only. `Adapter.total` keeps the first positive integer `TotalCount` seen, and `list_page` returns it every time (None until one is seen). A later 0 or missing value never replaces it.
- base.py is unchanged. The quirk is pailixiang's. A generic "ignore 0" rule in base would hide a real empty album on another platform. Slice 5 adapters should each check how their totals behave on later pages.
- Tests: `test_first_positive_total_count_is_kept` (0, then 200, then 0 → None, 200, 200) and `test_later_zero_total_count_still_reports_missing` (AlbumDownloader over 170 listed photos, 200 on page 1 and 0 afterwards → `{"done": 170, "missing": 30}`). Both fail on f6ad978's adapter (checked by swapping the file in from `git show`, then restoring and checking sha256).
- Touches: `src/photofinder/sources/pailixiang.py`, `tests/test_pailixiang.py` (FakePlx `total_for` hook).

## Slice 4 · Real proof — live pailixiang album on a scratch root (orchestrator)

Scratch root: `PHOTOFINDER_DATA_ROOT=/Volumes/Ext1TB/Projects/photo-finder-scratch/2026-09-29-plx/data` (real CLI `uv run --frozen photofinder …` from the worktree; the live `data/` — races.json, yipai/, exports/ — untouched). Logs: `…/2026-09-29-plx/download-run{1,2}.log`.
- `race add 2026-plx-scratch "PLX scratch"`; `album add 2026-plx-scratch https://live.pailixiang.com/album/a13800138000` → 1 AlbumGetView request, registered `pailixiang-a13800138000` titled `2026FUGA贡嘎100冰川极境赛`.
- Run 1 (f6ad978 code, A6 pacing): 9 pages, 715/715 done, exit 0, 757 s (~1 photo/s), no warnings/retries/403s; 715 files, 1600px previews. It exposed `TotalCount 0` on pages ≥2 (`done 160/0`) → fixed in bfc41a1 (above).
- Run 2 (bfc41a1): every page `fetched 0, done 715/715`, finished `{'done': 715}`, exit 0, 52 s (listing + page delays only).
- Scan only (no model stages; `stages.scan` via Python on `races/2026-plx-scratch`): 715 new, 0 errors, 0 skipped; 715 with `taken_at`/`taken_ts`, and all 715 equal the catalog's `taken_at` (sample preview has no EXIF DateTimeOriginal); range 2026-09-24 19:26:30 → 2026-09-27 06:01:10; `album` = `2026FUGA贡嘎100冰川极境赛` / `album_key` = `pailixiang-a13800138000` on all rows; `grp` null; all 715 uids `pailixiang:`-prefixed (top photographers 摄影师甲 176, 摄影师乙 137, 摄影师丙 119); index `source_photo_id` set == catalog `source_id` set; catalog 715 done; meta `title`, `album_id=26456848041475797975`.
- Not shown here: "Album filter shows both albums" and the time filter over both — the scratch race has one album; that is the live step below.

**Deferred — HUMAN-ACTION STEP (after the Slice 3 live import)**, from the main checkout after merging:
1. `uv run photofinder album add 2026-gongga100 https://live.pailixiang.com/album/a13800138000`
2. `scripts/download.sh 2026-gongga100` (yipai top-up first, then the 715 pailixiang photos, ~13 min for pailixiang).
3. `uv run photofinder index 2026-gongga100` (one heavy job; the 715 new photos go through detect/embed) → Album filter lists `FUGA 贡嘎100` and `2026FUGA贡嘎100冰川极境赛`.
4. Delete the scratch root `/Volumes/Ext1TB/Projects/photo-finder-scratch/2026-09-29-plx` (~0.6 GB) once satisfied.
- CLAUDE.md test count (now 422 passed + 1 opt-in) and a `album add` line in its layout/rules: left for the user (agents don't edit CLAUDE.md).

## Slice 4 · Whole-run gate fix

**Decisions**
- pailixiang: an `ID` that is None or absent is unusable (previously `str(None)` = "None" passed SAFE_ID, so two such rows would both write `photos/None.jpg`). It is skipped with a warning that names its `Name`.
- base: `download()` calls `preview_url(row)` once, after the file check. A falsy URL returns `failed`/`base.NO_URL` ("no preview url") before any sleep or request, and does not count toward the breaker (as yipai's "no s1920 url"). The row stays in the catalog, so a later listing that has a URL retries it. Before, `client.get(None)` raised TypeError out of `pool.map` and aborted the album.
- The URL is now taken once per download instead of once per attempt. Same result because `preview_url` must be pure (task 1 contract). A fresh URL still comes only from the 403 re-list.
- pailixiang total = first positive TotalCount minus the distinct skipped rows. The set key is (str(ID), FileName, Name), so a re-list or rerun doesn't subtract twice, and two None-ID rows with different files count twice. An album whose only gap is skipped rows finishes without `missing`, and `download` exits 0. The warning log still names each skipped row.
- README: xxpie/photoplus `album add` also stores no title until their adapters land (slice 5).
- Tests (all 4 fail on 5b14ca3's pailixiang.py/base.py, checked by swapping the pre-fix files in and restoring with sha256 match): `test_missing_or_null_ids_are_skipped`, `test_skipped_rows_leave_no_missing_gap_even_after_a_relist`, `test_row_without_preview_url_fails_alone` (test_pailixiang); `test_rows_without_preview_url_fail_without_tripping_the_breaker` (test_base_downloader, 3 URL-less rows with breaker 2 → no Blocked).

**Rejected**
- Keeping URL-less rows out of the catalog in the adapter: the gap would then show as `missing` forever. A `failed` row with a reason is visible and retried.
- Subtracting a per-page skipped count: a re-list of the same page would subtract twice.

**Deferred**
- A URL-less row still makes `download` exit 1 (non-done count), which is the intended "not every photo downloaded" signal. The row resolves only if the site later lists a URL.

**Touches**
- `src/photofinder/sources/base.py` (`NO_URL`, `download()` — shared by slice 5 adapters), `src/photofinder/sources/pailixiang.py`, `tests/test_pailixiang.py`, `tests/test_base_downloader.py`, README.md.

**Gate result (orchestrator)**: whole-run reviewer APPROVE after one fix cycle (2f43047); Codex 1 MAJOR (None ID) fixed, 1 MINOR (DDL before lock) already deferred. Full suite 426 passed, 1 skipped. Residual, deferred as low-likelihood: two ID-less rows with identical FileName/Name collapse to one skipped entry (rerun reports missing 1); a no-preview-URL row keeps `download` exiting 1 until the site lists a URL. Operational: the live `album add 2026-gongga100 …` + download + index runs after the Slice 3 live import (steps in the Real proof entry).

implement-loop: slice 4 shipped 2f43047; remaining: [5, 6, 7]


## Slice 5 · Task 1 — xxpie adapter

**Decisions**
- Token renewal is lazy. `check` (`Adapter.ok`) clears `self.token` on a non-zero `code` and raises ValueError. `fresh` (`Adapter.auth`) registers a new visitor when the token is None, so each retry carries a new token. TASK DEVIATION: the task said "renew in the check path". Doing it lazily means no registration is wasted after the last failed attempt: `tries` failures cost `tries` registrations. A test pins this (3 tries → tok1/tok2/tok3, 3 registrations).
- The token is sent per request (`fresh` headers), not on the client. Image GETs go without it, like the probe's curl fetches.
- Register: `fresh` builds a new `uuid4().hex` username on each attempt. The check needs `code == 0` and a non-empty `result.token`, else ValueError (retried). A registration that exhausts its retries raises from inside `fresh`: RetriesExhausted/Blocked are not caught by fetch_json, so the outer call raises at once (no nested retry loop).
- `platform=H5` is a query param on every GET. Register sends it in the JSON body only, as probed.
- Total: `meta()` reads `querySubAlbumPhotoInfo.photo_count` (any non-bool int, 0 included). `list_page` returns `photo_count - len(skipped)` (None if unknown), so skipped rows don't show up as `missing`. This is the pailixiang fix applied to a total that comes from meta. The listing's `count` (always 0) is never read.
- `photographer_uid = photographer.team_id`. In the fixture, `upload_by` is absent from every listing row, and `team_id` 61a8abc6… equals Photographer E's `sys_user_id` in `upload_bys`. A test checks every sample row against `upload_bys`.
- `record_time`: must fully match `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z`, then `fromisoformat` (py ≥3.13 accepts Z) → Asia/Shanghai → `%Y-%m-%d %H:%M:%S` (ms dropped). Anything else, including an impossible date, becomes None.
- Cursor = page_no int (None → 1). Next = page+1 while `len(photos) >= 60`. A full last page costs one extra empty request.
- No `meta_items()`. The manifest `meta` keeps only `title`.
- `races.parse_url` for xxpie takes `album_id`, else `id`. The user's `/m/album?id=…` URL now works; `?id=../x` is still refused by SITE_ID.
- Fixtures: `xxpie_list.json` and `xxpie_subalbum_info.json` are verbatim copies of the probe files. `xxpie_register.json` (token REDACTED, icon URL dropped) and `xxpie_style.json` (trimmed) come from 3 live requests, ≥2.5 s apart; the same trimmed copies are in the probe folder.

**Rejected**
- Storing the visitor token in `meta` (spec §4.2 names it as an example): nothing reads `meta` back (the adapter contract has no read path), so it would be a bearer credential on disk that nothing uses. Registering once per run costs a single request.
- Registering inside `check` (the literal task wording): it wastes a registration after the final failed attempt, and it nests a network call inside response validation.
- Using `upload_by` for the uid: it isn't in the listing.

**Assumptions**
- The body shape of a non-zero `code` is unverified. `queryAlbumStyleH5` answered `code 0` with a garbage token (live), so the style call doesn't depend on the token. Renewal is tested only against a synthetic `{"code": 401, "message": …}`. The listing endpoint was outside the probe allowance. To check: a real download log would show `xxpie code …` warnings.
- `album add` costs 3 requests: register, style, subalbum info.
- `photo_count` (3670 at probe) equals what the ALL sub-album listing returns. Any gap shows as `missing`.

**Deferred**
- Real xxpie download and index of Chongli: the orchestrator runs them (register `2026-chongli168`, then `album add … https://www.xxpie.com/m/album?id=65178998a458227944415097`).
- Task 2: `test_download.test_unsupported_platform_album_is_skipped` now uses a photoplus URL. Once photoplus registers, no platform is unsupported, so the test must monkeypatch `cli.ADAPTERS` (e.g. drop a key) or be deleted.
- `site_link` for xxpie: Slice 7.

**Touches**
- New: `src/photofinder/sources/xxpie.py`, `tests/test_xxpie.py`, `tests/fixtures/xxpie_{list,subalbum_info,register,style}.json`, `docs/handoff/2026-09-29-platform-probes/xxpie_{register,style}.json`.
- `src/photofinder/cli.py` (`ADAPTERS["xxpie"]`, import), `src/photofinder/races.py` (`parse_url` xxpie `id`), `tests/test_races.py` (`?id=` accept/reject cases), `tests/test_download.py` (skip test → photoplus), README.md (title fetch and download lines).


## Slice 5 · Task 2 — photoplus adapter

**Decisions**
- `sign(params, t)`: nulls dropped, bools sent as `"false"`/`"true"`, `_s` over the sorted `k=v` of JSON values with quotes stripped. The whole signed query is built in `fresh` from `clock()` (new `clock=time.time` kwarg), so each attempt gets a new `_t`. `check` requires `code == 1` and returns `result` unchanged (a list for `/album/albums`, a dict elsewhere).
- Cursor: `("album", i, page)` → `("list", page)` → None. `None` resolves to `("album", 0, 1)`, or `("list", 1)` when there are no sub-albums. The `/album/albums` answer is cached per run, so a re-list refetches only the page.
- First group: `first_seen[id]` holds the cursor where the id was first listed. A row is emitted only when that equals the current cursor. A re-list of the same cursor re-emits it; a later sub-album or `/pic/list` page never does. The listing order is deterministic, so a rerun gives the same first groups.
- Total, learned without an extra request: `/pic/list` page 1 is always the first reconcile page (it emits only unseen ids) and gives `pics_total`. The first positive non-bool int is kept (a later 0 or different value is ignored). The list phase continues while `page < ceil(total/100)` and seen+skipped < total. If the total is unknown, it continues while pages are full. `meta()` = `/live/detail` only, so `album add` costs 1 request.
- Sub-album pages: 200 per page; next page while full (probe: page 3 of a 223-photo sub-album was `pics: []`).
- Photographer = `camer`/`camer_no` when `camer` is set, else `retoucher`/`retoucher_no`. In 89243825 `/album/one`, camer is filled (three names) and retoucher is `Photographer B` on every row (an editor). In 39352660, camer is null.
- `HEADERS` = Referer `https://live.photoplus.cn/` + desktop UA. Checked live with the real adapter: `meta()` + the first `list_page` on 89243825.

**Rejected**
- Fetching `/pic/list` page 1 in `meta()` for the total: it doubles `album add` cost and throws away 100 rows.
- Carrying `key` in the cursor: `key=""` pages 1/2/22 are disjoint (probe), so `key` isn't needed.
- Stopping `/pic/list` on a short page: the spec says `ceil(pics_total/100)`, and `pageTotal` is wrong (2.0 for 22 pages).

**Assumptions**
- Live probe (15 GETs, ≥2.5 s apart). 89243825: 8 sub-albums with Σpic_num 2156 = pics_total. Two sub-albums fully listed (放松跑 223, ACG大本营 146) share no ids. So there the sub-albums look disjoint and complete, and the list phase costs 1 request. 39352660: 16 sub-albums, Σpic_num **3590 vs pics_total 6115**, so ~41% of photos are in no sub-album and get group null. The list phase walks all 62 pages there.
- `/pic/list` with `key=""`: p1 (100) / p2 (100) / p22 (56 = 2156−2100) disjoint, p23 empty. `pics_total` is present and stable on every page, including the empty one. Only page 1 has `key`.
- The early stop (seen+skipped ≥ pics_total) assumes that every id in a sub-album is also counted in `pics_total`. If a sub-album holds hidden photos, the list phase could stop early and the gap would not show as `missing`. That is unlikely given 89243825's exact Σ match. `/album/albums` is assumed to return a list (true on both activities probed). A dict would raise on the first `list_page`.
- `/album/one` shape: `result.{pageTotal, album, pic_total, pics}`. Row fields match `/pic/list` plus `album_pic_id`.
- `big_img` downloaded 200 with no headers at all (1600×1067 JPEG), so no Referer is needed for images. How long the signature stays valid is unmeasured; the 403 re-list covers it.
- `/live/detail` has `anti_crawler_level` (value in `photoplus_detail.json`). What it controls is unknown. Watch the first real run for code -1 or 403 bursts.
- 30326728 (四姑娘山, 92,889) was not probed. If it is like 39352660, listing alone takes ~930 `/pic/list` + ~465 `/album/one` pages. At page_delay 3–6 s that is ~1.5–2.5 h of pacing on top of the downloads. One `/album/albums` request (Σpic_num vs 92,889) would tell.

**Deferred**
- Photos in several sub-albums keep only the first group (spec §7).
- The `raw None cursor` mutant survives. It is equivalent: stored None never equals a later non-None cursor, so behaviour is the same.

**Touches**
- New: `src/photofinder/sources/photoplus.py`, `tests/test_photoplus.py`, `tests/fixtures/photoplus_{detail,albums,list,album_one}.json`. Probe copies in `docs/handoff/2026-09-29-platform-probes/photoplus_*.json` (also `albums_39352660`, `list_p2`, `list_p23`, `album_one_p3`; signed query strings stripped).
- `src/photofinder/cli.py` (`ADAPTERS["photoplus"]`, import), `tests/test_download.py` (skip test drops photoplus from `cli.ADAPTERS`), README.md (title fetch and download lines).


## Slice 5 · Whole-run gate fix

**Decisions**
- photoplus first group: `group_of[id]` (the first group) replaces `first_seen[id]` (the first cursor). Every safe listed row is emitted, carrying `group_of.setdefault(id, group)`. The upsert never sees a second group for an id within a run, so the first-group rule holds. A photo that 403'd and then shifted to the next page before the re-list is fetched from the later listing (Codex MAJOR 1). `rows()` lost its dead `cursor` arg.
- Cost of re-emitted rows (checked in base.py): `_todo` → `is_done` (sqlite select + `jpeg_file_ok`). `download()` also checks `jpeg_file_ok(dest)` before any request. So a re-emitted **done** row costs an upsert and a file-header check, with no HTTP. A re-emitted **failed/pending** row gets a fresh download attempt, which is the intended repair. `test_downloader_fetches_every_photo_once…` still sees every image fetched exactly once across 2 runs.
- photoplus stop rule (seen = `len(group_of) + len(skipped)`): total unknown → page while full. seen == total → stop. seen > total → warn once ("seen N distinct photos vs pics_total T") and page while full, not bounded by `ceil(total/100)`. seen < total → `page < ceil(total/100)` as before. The returned total is still `pics_total - skipped`, so an undercount never produces a false `missing`.
- The `==` branch comes before `>`, so an `==`→`>=` mutant stays live (it was caught).
- xxpie: a 403 re-list test (fresh `l=2` URL → done, each image fetched twice) and a breaker test (persistent 403, `concurrency=1`, `max_consecutive_failures=2` → Blocked). The fake tags URLs only when `expire_first_listing` is set, so the fixture-URL assertion is unchanged.

**Rejected**
- Keeping the per-cursor filter and adding a special case for shifted ids: the one-group-per-id map already guarantees the rule, and the special case adds state.
- (Orchestrator) Subtracting skipped invalid-id rows from pics_total in the stop rule, i.e. changing the Slice 4 treatment. It was gate-approved in Slice 4; skipped rows already count toward `seen`.
- (Orchestrator) Having xxpie clear the token only on specific codes: the body shape of a non-zero code is unverified (Task 1 assumption). One wasted registration on a non-auth error is cheap, and a stale token is worse.
- Stopping at `ceil(total/100)` even when seen > total: a total that undercounts what was already seen can't bound the listing.

**Assumptions**
- When seen > pics_total, `/pic/list` still ends with a short (<100) page, as probed (p22 = 56, p23 empty). If the site returned full pages forever, the base same-cursor guard would not trip, because the cursor advances. Check: the first real run on an album that logs the warning.
- Re-emitting reconcile rows makes photoplus upserts rewrite metadata (fname/photographer/taken_at) from `/pic/list`, which may differ from `/album/one`. The fixtures show the same fields. Check: diff one id's row across the two endpoints.

**Deferred**
- The Task 2 note "raw None cursor mutant survives" no longer applies: `first_seen` is gone.

**Touches**
- `src/photofinder/sources/photoplus.py` (`group_of`, `warned`, `rows()` signature, stop rule).
- `tests/test_photoplus.py`: FakePP `upload_on_expiry`. Updated shape assertions. New tests: shift, over-total.
- `tests/test_xxpie.py`: FakeXx `listings`/`expire_first_listing`/`image_status`, 2 new tests.
