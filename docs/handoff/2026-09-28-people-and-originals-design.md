# Handoff — 2026-09-28-people-and-originals-design

Design: `docs/superpowers/specs/2026-09-28-people-and-originals-design.md`. Earlier build log: `docs/handoff/2026-09-27-photo-finder-search-design.md`.

## Slice 1

Slice 1 SLICE_BASE=e495fed

Tasks: T1 profiles schema + migration + per-profile labels/find-more/my-photos/export + matched-via (M, risk: yes — "data migrations", reviewed) · T2 originals job + endpoints (L, risk: yes — "concurrency/locking": one background job per collection with cancel, reviewed) · T3 UI + real-browser E2E on a copy of the live index (M, risk: no, last task → lean, whole-run gate reviews it).

Run-wide decision: `tests/test_web.py::test_page_is_served_and_calls_only_real_endpoints` checks page `/api/` calls == app `/api/` routes both ways. T1 and T2 add routes the page does not call yet, so they relax it to page ⊆ app; T3 restores both directions once the UI calls every route.

## S1-T1 — profiles schema + migration + per-profile labels/find-more/my-photos/export + matched-via

**Decisions**
- Schema: `profiles(id pk, name text unique not null, created_at)`; `labels(profile_id → profiles, person_id → persons, label me|not_me, created_at, pk(profile_id, person_id))`. DDL lives once in `db.PROFILES` / `db.LABELS` (format slot = table name), shared by SCHEMA and MIGRATE.
- Migration `db.migrate` runs in `connect` before `executescript(SCHEMA)`: cheap `pragma table_info(labels)` check outside a lock, then `begin immediate`, re-check, run `db.MIGRATE` (create profiles, insert "Me", create labels_new, copy with created_at, drop old, rename), commit; any exception → rollback + re-raise (connect fails loudly rather than open a half-migrated index). With `foreign_keys=on` drop/rename are clean: nothing references labels, and the persons/profiles FKs survive the rename (asserted via `pragma foreign_key_list`).
- Empty-state rule: `connect` inserts profile "Me" (`insert or ignore`, then commit) whenever `profiles` is empty, so fresh DBs get Me = id 1 and the UI always has ≥1 profile. `DELETE` of the last profile → 400 "cannot delete the last profile; create another one first".
- `profile_id` is required (type `Id`) on search body, labels body, export body, `/api/me` and `/api/photos/{id}` query; unknown → 404 `profile N not found`, checked before any other search validation. Missing → 400 (validation handler).
- `/api/facets`: optional `?profile_id=`; `labels {me, not_me}` only present when given (scoped). Kept because the current page's local count code reads `S.facets.labels`; T3 may switch to `/api/profiles` counts.
- Names: strip, 1..40 chars; duplicates are case-sensitive (SQLite default `unique`), caught as IntegrityError → 400. Rename to own name is fine.
- Export: `DATA_ROOT/exports/<collection>-<safe_name(profile)>-YYYYMMDD.txt`; `safe_name` = `re.sub(r"[^\w-]+", "_")` stripped of `_`, fallback `profile` (keeps CJK). Same-day overwrite is now per profile.
- matched_via rule (`search.matched_via`): only for osnet/siglip reference rows (person refs + upload row), on the returned page only; per result, argmax over refs of `0.3·cos_osnet + 0.7·cos_siglip` (WEIGHTS, raw cosines, NaN → 0). Winner is a person → its id; winner is the upload row → null; < 2 refs (single person, upload only, text/scene only, bib start) → null. Text/scene terms never compete. Ranking/scores untouched.
- Ref-id mapping: `ref_ids = persons.ids[np.isin(persons.ids, ids)]` — `person_refs` returns rows in persons order (deduped), not request order; the upload row is appended last.
- Page (`index.html`): init fetches `/api/profiles`, uses `profiles[0].id` for every call. No UI added. Drift test relaxed to `used <= routes`.

**Rejected**
- `create table if not exists labels` + `alter table add column`: can't change a primary key; old shape would be kept silently.
- `executescript` for the migration: it commits first and per statement, so no rollback.
- Always `begin immediate` in connect: every request would wait on an indexer's write lock.
- Dropping facets `labels` for `/api/profiles`: bigger page change now; T3 rewrites the page anyway.
- `collate nocase` on name: deviates from the specified schema; case clash recorded under Deferred.
- matched_via by osnet-only or siglip-only argmax: they disagree; the fused weights are the ranking's own trust split.
- Implicit default profile when `profile_id` is omitted: hides scoping bugs; the spec says required.

**Assumptions**
- `cli.py` (index/search/eval) also opens via `db.connect`, so the migration and Me-insert run there too; nothing in cli/evaluate reads labels. Check: `grep -n labels src/photofinder/cli.py src/photofinder/evaluate.py`.
- Old live labels all reference existing persons (old table had the same FK with FKs on). If an orphan exists, connect raises IntegrityError and the old table stays intact (tested).
- `created_at` for the new profile uses SQLite `localtime`, same format as `stages.now()`.

**Deferred**
- Migration on a copy of the live index (acceptance 1, "10 me labels") not run here — task forbids touching the live dir; T3/orchestrator do it on a copy.
- `safe_name` collisions ("A/B" vs "A_B", "Me" vs "me" on case-insensitive APFS) share an export file name; T2 should key folders by name + id if that matters. `safe_name` lives in `web/app.py`; T2 may move it.
- ONE-WAY MIGRATION (for the human): once merged code opens the live index, the old master server (port 8000) still writes `on conflict(person_id)` and every label write fails. Restart the port-8000 server on merged code right after the first merged-code connect.

**Touches** (API shapes for T2/T3)
- `GET /api/profiles` → `{"profiles": [{"id", "name", "me", "not_me", "me_photos"}]}` (order by id; me/not_me = person label counts; me_photos = distinct photos with a me person).
- `POST /api/profiles {name}` → profile object · `PATCH /api/profiles/{id} {name}` → profile object (400 dup/empty/>40, 404 unknown) · `DELETE /api/profiles/{id}` → `{"id", "labels_removed"}` (404 unknown, 400 last).
- `POST /api/labels {profile_id, person_id, label|null}` → `{"profile_id", "person_id", "label", "previous"}`.
- `POST /api/search` body adds required `profile_id`; every result adds `matched_via` (person id | null), bib start included.
- `GET /api/photos/{id}?profile_id=` · `GET /api/me?profile_id=` · `POST /api/export {profile_id}` → `{"path", "count"}` · `GET /api/facets[?profile_id=]`.
- Files: `src/photofinder/db.py` (SCHEMA, PROFILES, LABELS, MIGRATE, DEFAULT_PROFILE, migrate, connect), `search.py` (+`matched_via`), `web/app.py`, `web/static/index.html`, `tests/test_web.py`, new `tests/test_db.py`.

**Evidence**
- `uv run pytest -q`: 215 passed (base was 206, not 202; +4 `tests/test_db.py`, +5 `tests/test_web.py`). `node --check` on the extracted page script OK.
- Mutations 9/9 caught (copy insert dropped, default-profile commit removed, rollback removed, prepare/labels_of/me_rows unscoped, argmax→argmin, osnet-only sim, ref ids in request order); restored from copies, sha256-verified.

**T1 review (orchestrator)** — fresh reviewer: 0 CRITICAL / 0 MAJOR, 215 passed; all 8 load-bearing claims held (single-transaction rollback, two-process migrate race, FK survival, scoped queries, ref-id order, matched_via on the page only). Ledger: MINOR unknown profile + bad date/scene → 400 before 404 — rejected, the page always sends a listed profile id and 400 still names the problem; safe_name collisions ("Ann B" vs "Ann_B") → routed to T2, which owns per-profile folders; text term never competes for matched_via — as designed (matched_via names a person reference); first connect during an indexer write lock may 500 once, raw IntegrityError on a failed migration — deferred (one-time, self-heals / loud by design).
- Orchestrator real check on a disposable copy of the live index (`data/subsets/fullcopy`, `.backup` from a read-only source): migrate twice → profile 1 "Me", 10 `me` labels, person_id/label/created_at identical to before, integrity_check ok, foreign_key_check empty.
