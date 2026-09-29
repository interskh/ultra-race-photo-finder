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
