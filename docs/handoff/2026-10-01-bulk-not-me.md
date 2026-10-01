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

## Gate fixes (round 2)

**Decisions**
- View change (Search <-> My photos) no longer retires the batch or bumps `S.bepoch`: the search cards are the same cards on return, so a response that lands meanwhile still paints them. `showView` still wipes the banners (existing behaviour), so the Undo button is gone after a round trip; the paint and counts are kept. Epoch bumps only on new batch, search(false), profile switch, race switch.
- Count sync is serialised with single-label requests: `syncCounts()` sets `wantSync`; `pumpSync()` fetches `/profiles` only when no label request is pending, and discards the result and retries if one started during the fetch (`S.lseq` bumps on every single-label send and on every batch/undo delta). `setLabel`'s finally pumps. Chosen over "skip delta if a sync finished after send": the sync's read can precede the label's commit, so skipping can lose the delta; waiting for quiet always converges to the server.

**Rejected**
- Keeping Undo across view round trips: banners are cleared per view by existing design; not worth a second mechanism.

**Touches**
- `index.html` only (showView, syncCounts/pumpSync, setLabel, state `lseq/wantSync/syncing`). Harness `smoke_bulk.js`: 2 new scenarios, both fail on the round-1 page; mutants (no pending guard, epoch bump on view) caught.

## Whole-run gate result (orchestrator)

- Fresh reviewer: approve after rounds 1 and 2. Codex: NO-SHIP after round 2 with two open MAJOR UI races; the 2-round gate cap was reached, and the owner chose to merge with them as known issues (2026-10-01).
- **Known issues (open):**
  1. Undo clicked, then Search → My photos → Search before its response: the server deletes the batch rows, but `showView` cleared `S.bulk`, so the cards stay painted Not me and are not bulk candidates until the next search (counts resync correctly).
  2. A second batch started while a stale-batch count sync is fetching `/profiles`: if the fetch already includes the new batch's commit, `lseq` still matches, the sync applies the new total and the batch response adds its delta again (Not me over-counted until the next sync/search). Batch requests lack the single-label pending guard.
  - Both need a click inside one in-flight request (~50 ms locally); display-only, no data loss. Proposed fix: drop local deltas for batch/undo and always resync counts from `/profiles`; paint keyed on the epoch only, not `S.bulk` identity.
- Full suite 586 passed, 1 skipped (06bd986).
- Real-browser E2E (playwright, worktree server on `data/subsets/race925`, user's server stopped and restored; 0 console errors; screenshots `data/exports/screens/bulk-0{1..3}-*.png`): bib 8039 page button reads "Not me: the other 25 nearby" (bib hits never counted); 3 bib cards Me → Find more → 4 more bib-8039 cards Me → "Not me: the other 59" → 59 cards painted, Not me pill 59, banner with Undo; Undo → 0 painted, pill 0; batch again → Find more: 0 of the 59 dismissed photos back, summary "59 hidden (Not me: the other)"; a similar search (top 200) still returns 8 of them. All E2E labels removed afterwards (race925 labels 0 / 0). The race925 index now has the migrated `hidden` column.
