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
