# Bulk "Not me: the other N" — handoff

## T1 — backend (labels.hidden, batch + undo, Find more / nearby exclusion)

**Decisions**
- `labels.hidden integer not null default 0` in the `LABELS` template (fresh DBs) + `MIGRATE_HIDDEN` via `once(db, no_hidden, ...)` after the legacy-labels migration (legacy path creates the table through `LABELS`, so it already has the column and `no_hidden` is false). `no_hidden` is false when the table is absent.
- Mark: `POST /labels/batch {profile_id, person_ids}` -> `{profile_id, changed: [pid], skipped: [{person_id, reason}]}`; reasons `"already me"`, `"already not_me"`, `"photo has a me person"` (label check first). Ids de-duplicated, order kept. Unknown person -> 404 before any write; unknown profile 404. One `begin immediate`.
- Undo: dedicated `POST /labels/batch/undo {profile_id, person_ids}` -> `{profile_id, removed, kept}`. Deletes only rows that are still `not_me` + `hidden=1`, so a label changed afterwards (Me, single Not me, cleared) is never touched. T2 passes the batch's `changed` list.
- Single `set_label` upsert sets `hidden = 0` (explicit Not me / Me = person-level only); clearing deletes the row (unhides).
- Find more (`mode == "more"` only): hidden photos (>=1 not_me+hidden person for the profile) join the `exclude` set (the rank base `offset + len(set(seen))` pre-existed; unchanged). Response gains `"hidden": N` (hidden photos this Find more actually left out: hidden photos that pass the current filters and are not already excluded as Me photos; seen/exclude_photos are not subtracted) in more mode only; absent otherwise.
- `/nearby`: `NearbyQuery.hide_hidden: bool = false`; `nearby.collect(..., hide_hidden)` drops those photos as candidates. Bib page keeps default false. `/neighbors`, bib start, similar/text/scene/upload, My photos untouched.
- Aggregates (`/profiles`, `/facets`) and negatives unchanged: batch rows are plain not_me.
- Tests: `tests/test_bulk_not_me.py` (17). Fixed 4-value `insert into labels` / `select *` in test_race_import/test_db; test_web page-routes test allows the two new routes unused (T2 must shrink back to `used == routes`).

**Rejected**
- Undo as `labels/batch {label: null}`: would also unhide anything a bulk-null means; a dedicated route has an unambiguous guard.
- `label`/`hide` fields on the batch body: only one batch kind exists; speculative.
- A separate hidden table: needs its own cleanup on profile delete / race import; a column rides along with labels.
- Excluding hidden photos in every search mode / nearby by default: spec limits it to Find more.
- Adding `hidden` to race_import's label snapshot: it never edits labels; the snapshot compares before/after of the same DB.

**Assumptions**
- Page size keeps `person_ids` well under SQLite's variable limit (32766); `MAX_TOP` bounds a page.
- Without the flag, `/nearby` already drops a photo whose only persons are not_me; the flag matters when a second unlabelled person exists (tested on photo 4 of the roll fixture).

**Deferred**
- UI (T2). README/ROADMAP/CLAUDE.md docs and test count (566 -> see run) left to docs step.
- (fixed in gate round 1) Undo had no batch identity; see Gate fixes.
- `race_import.snapshot` omits `hidden`: import never edits labels, and it compares the same DB before/after.

**Touches**
- `src/photofinder/db.py` (LABELS, MIGRATE_HIDDEN, no_hidden, migrate), `src/photofinder/nearby.py` (`collect` signature + labels query now 4-tuples), `src/photofinder/web/app.py` (BatchBody, NearbyQuery.hide_hidden, prepare/ranked, two routes, set_label), `tests/{test_bulk_not_me (new),test_db,test_race_import,test_web}.py`.
- Public API: `POST /api/r/{slug}/labels/batch`, `.../labels/batch/undo`, `hidden` on Find more responses, `hide_hidden` on `/nearby`. Schema: `labels.hidden`.

## T2 — UI (button, batch + Undo, Find more hidden count) and docs

**Decisions**
- Button `#bulk` ("Not me: the other N") in the sticky bar left of Find more; hidden until a search exists, disabled with an explanatory title when N=0, busy, or not the search view. Title says the hide effect is Find-more-only.
- N = distinct person ids of cards in `S.results` + visible nearby section with no label, photo not in `S.mine`, not mid-label (`S.pending`). `renderBulk()` runs from `syncControls`/`renderCounts` and `setLabel`'s finally (pending is cleared only there; without it N lagged one click).
- Batch click uses `setBusy` (dims grids, disables controls), guards on profile+`S.gen` after the await, paints via `paint()`, bumps `S.facets.labels.not_me`; cards stay put. Skipped ids untouched; count mentioned only if non-zero.
- Undo state `S.bulk {profile, gen, changed, el}`; banner buttons carry `data-run` so they disable while busy. `retireBulk()` (removes every `[data-bulk]` banner incl. the "Undid" note) runs on new batch, `search(false)`, `switchProfile`; `clearRace` and `showView` change reset it with the banners.
- `hide_hidden: S.base.mode === 'more'` lives in `nearBody()`, so it covers the section refetch (stepper/toggle) too; bib page sends false. `S.hiddenN` from non-append Find more responses, shown as "N hidden (Not me: the other)".

**Rejected**
- Hiding/removing the cards on success: spec says paint in place; next Find more excludes them.
- Keyboard binding: no natural conflict-free key.
- Disabling when no Me mark yet: spec lists only N=0/busy/view; Undo covers a misclick.

**Assumptions**
- Photo "has a Me person" = `S.mine` (loaded with the profile, kept in sync by `setLabel`); the server re-checks and skips otherwise.
- `S.pending` entries are single-label requests in flight; excluding them from N is safe.

**Deferred**
- Real-browser E2E (orchestrator). Modal strip not re-evaluated after a batch (batch only adds Not me on unlabelled persons; strip anchors need a Me mark).
- Undo identity issue from T1 remains (UI only offers the latest batch).

**Touches**
- `src/photofinder/web/static/index.html` (CSS `.bulk-wrap`, `#bulk-wrap`, state `hiddenN`/`bulk`, search/summary/nearBody/setLabel/switchProfile/clearRace/showView), `tests/test_web.py` (route test back to `used == routes`, new page test), `README.md`, `docs/ROADMAP.md`, `CLAUDE.md` (584 passed).
- Smoke harness: scratchpad `smoke_bulk.js` (25 checks; 7 FAIL/ERR lines on the pre-change page; mutants 6/6 caught).

## Gate fixes (round 1)

**Decisions**
- Bib page: `otherIds()` counts only the nearby section there (bib hits are mostly the user); button reads "Not me: the other N nearby", tooltip says exact bib reads are never touched, banner says "N nearby". Other modes unchanged.
- Batch epoch `S.bepoch`, bumped inside `retireBulk()` (new batch, search(false), profile switch, race switch, view change). Batch/undo responses from an older epoch have no UI effect; `syncCounts()` refetches `/profiles` for authoritative Me/Not me counts (covers A-B-A and view-change drift).
- Per-person ordering: batch response paints a person only if its local label is still null and nothing is pending for it; undo only if still `not_me` and not pending. Counts use ALL `changed`/`removed` (the DB did change them; a later single-label response reports previous=not_me and decrements, so applying only painted ones would drift negative).
- Undo identity without a column: the batch stamps all its rows with one microsecond `created_at` and returns it as `batch`; `/labels/batch/undo` now requires `{profile_id, person_ids, batch}` and deletes only rows with that `created_at` AND not_me AND hidden=1. `begin immediate` serialises batches, so two batches can't share a microsecond.

**Rejected**
- A `batch_id` column: schema change + migration for what created_at already carries.
- Second-precision `now()` as the token: two batches on one person within a second (scripts, fast retry) would collide.

**Assumptions**
- Nothing reads label `created_at` besides race_import's before/after snapshot, so the microsecond format is harmless.

**Touches**
- `web/app.py` (UndoBody, batch stamp, undo filter), `index.html`, `tests/test_bulk_not_me.py` (19; two-batch regression, token required), `tests/test_web.py`, README. API: undo body gains required `batch`; batch response gains `batch`.
