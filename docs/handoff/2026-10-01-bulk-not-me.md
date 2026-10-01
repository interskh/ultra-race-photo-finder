# Bulk "Not me: the other N" — handoff

## T1 — backend (labels.hidden, batch + undo, Find more / nearby exclusion)

**Decisions**
- `labels.hidden integer not null default 0` in the `LABELS` template (fresh DBs) + `MIGRATE_HIDDEN` via `once(db, no_hidden, ...)` after the legacy-labels migration (legacy path creates the table through `LABELS`, so it already has the column and `no_hidden` is false). `no_hidden` is false when the table is absent.
- Mark: `POST /labels/batch {profile_id, person_ids}` -> `{profile_id, changed: [pid], skipped: [{person_id, reason}]}`; reasons `"already me"`, `"already not_me"`, `"photo has a me person"` (label check first). Ids de-duplicated, order kept. Unknown person -> 404 before any write; unknown profile 404. One `begin immediate`.
- Undo: dedicated `POST /labels/batch/undo {profile_id, person_ids}` -> `{profile_id, removed, kept}`. Deletes only rows that are still `not_me` + `hidden=1`, so a label changed afterwards (Me, single Not me, cleared) is never touched. T2 passes the batch's `changed` list.
- Single `set_label` upsert sets `hidden = 0` (explicit Not me / Me = person-level only); clearing deletes the row (unhides).
- Find more (`mode == "more"` only): hidden photos (>=1 not_me+hidden person for the profile) join the `exclude` set; the rank base is `offset + len(set(seen))` so ranks stay 1-based. Response gains `"hidden": N` (hidden photos this Find more actually left out: hidden photos that pass the current filters and are not already excluded as Me photos; seen/exclude_photos are not subtracted) in more mode only; absent otherwise.
- `/nearby`: `NearbyQuery.hide_hidden: bool = false`; `nearby.collect(..., hide_hidden)` drops those photos as candidates. Bib page keeps default false. `/neighbors`, bib start, similar/text/scene/upload, My photos untouched.
- Aggregates (`/profiles`, `/facets`) and negatives unchanged: batch rows are plain not_me.
- Tests: `tests/test_bulk_not_me.py` (16). Fixed 4-value `insert into labels` / `select *` in test_race_import/test_db; test_web page-routes test allows the two new routes unused (T2 must shrink back to `used == routes`).

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
- Undo has no batch identity: batch A hides p, user clears p, batch B re-hides p, undo A deletes B's row. Deferred: the UI only offers undo for the latest batch.
- `race_import.snapshot` omits `hidden`: import never edits labels, and it compares the same DB before/after.

**Touches**
- `src/photofinder/db.py` (LABELS, MIGRATE_HIDDEN, no_hidden, migrate), `src/photofinder/nearby.py` (`collect` signature + labels query now 4-tuples), `src/photofinder/web/app.py` (BatchBody, NearbyQuery.hide_hidden, prepare/ranked, two routes, set_label), `tests/{test_bulk_not_me (new),test_db,test_race_import,test_web}.py`.
- Public API: `POST /api/r/{slug}/labels/batch`, `.../labels/batch/undo`, `hidden` on Find more responses, `hide_hidden` on `/nearby`. Schema: `labels.hidden`.
