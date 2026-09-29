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
