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

## S1-T2 — originals job (fresh URL by file name, paced fetch, naming, CSV, zip, single photo) + endpoints

**Decisions**
- `yipai.request_json` (lifted; `Downloader._request_json` delegates) and `yipai.write_atomic` (`.part` → replace; Downloader uses it). Image loop lives in `originals.Fetcher.fetch`, same helpers/policy (HEADERS, primary/failover alternation, backoff_seconds, Retry-After on RETRYABLE), sequential so no cooldown/breaker state.
- Classification: image 403 / 200 body not starting `FFD8` / no `sign` / photoId absent from filtered set → `buy on site: <reason>`; 200 starting `FFD8` but failing `looks_like_jpeg` (truncated) and exhausted 5xx/network → `failed: <reason>`; other 4xx on the image → `failed: HTTP n` (no retry). Lookup API errors (request_json `Blocked`, incl. API 401/403) → job `state: error` (single photo: 502). Marked photo with no manifest row → `failed: not in the gallery manifest` (no network).
- Rerun retries everything not on disk (buy-on-site too: one lookup + one fetch each; handles "bought since").
- Pacing: `Fetcher.last_lookup` (per app, shared by job + single photo) → wait `last + 1 s − clock()` before every lookup page; image fetch preceded by `img_delay` (0.2–0.6 s). All waits go through `Fetcher.pause` = `sleep` (default `stop.wait`) then raise `Cancelled` if stop is set — also used as request_json's backoff sleep so retries never fire back-to-back after cancel.
- Lookup paging capped at 10 pages (1000 same-name photos) → `failed:` — guards a silently ignored `fileName` filter from paging the whole gallery per photo.
- Status source of truth: `downloaded` = file on disk; other statuses = `photos.csv` (rewritten atomically under `originals.CSV_LOCK` after every attempted photo, at job end/cancel/error, by export and by single-photo). A CSV `downloaded` whose file is gone reads as null. No DB table.
- One network job per collection: `Job.busy` Lock acquired non-blocking (check-and-set) by batch start, single photo, and folder-moving rename → 409. Rename/delete of the profile the job is running for → 409 regardless of whether its folder exists yet (the folder appears only after the first write).
- Names: `safe_name` (moved to `originals`) = NFC, `[^\w-]+`→`_`, strip `_`, ≤40 chars, fallback `profile`; `folder_key` = casefold of it. Create/rename rejects a name whose folder key matches another profile (400, message contains "folder"). Rename moves `exports/<coll>/<old>` → `<new>` inside the DB transaction (OSError rolls back); target exists → 400 (case-only rename is a plain rename). Delete leaves the folder.
- File: `<YYYYMMDD-HHMMSS|undated>_<safe photographer|unknown>_<source_photo_id>.jpg` ("undated" sorts after digits = nulls last like `FIRST`).
- Single-photo buy on site → **402** with `detail = "buy on site: <reason>"`; failed → 502.
- Zip: temp `.originals-*.zip` in the profile folder (Ext1TB, not /tmp), `originals/*.jpg` + `photos.csv`, ZIP_STORED, unlinked by BackgroundTask.

**Rejected**
- Instantiating Downloader (mkdirs photos/, opens manifest rw) or reusing `Downloader.download` (s1920 key, cooldown/breaker, "expired" relist semantics).
- `originals` table in index.sqlite: schema change + writes to the live index from a job thread; files+CSV already survive restart and follow a folder rename.
- Unconditional 1 s sleep between lookups: doesn't space rapid single-photo clicks across requests.
- Keeping T1's `<collection>-<safe>-YYYYMMDD.txt`: replaced by the CSV per spec.

**Assumptions**
- yipai collection ⇔ `<collection>/manifest.sqlite` exists; the real-run copy (e.g. `data/subsets/fullcopy`) must include it or originals → 400.
- `photoId` in the API is an int equal to manifest `photo_id` and index `source_photo_id` (probe shows int 58532728).
- A watermarked JPEG from a paid gallery would be saved as `downloaded` (not detected); this gallery is byte-identical to 下载.

**Deferred**
- Watermark detection (e.g. compare size/dimensions with manifest `size`/`width`) — only relevant for paid galleries.
- `/api/profiles` downloaded count (needs per-profile rows); T3 can count `original == "downloaded"` from `/api/me`.
- Zip is built fully before streaming (~360 MB for 100 originals, one temp file).

**Touches** (shapes for T3) — `src/photofinder/originals.py` (new), `sources/yipai.py`, `web/app.py`, `tests/test_originals.py` (new), `tests/test_web.py`, `README.md`.
- `create_app(collection, fetcher=None)`; `app.state.originals` = Job. `GET /api/facets` adds `originals: bool`. `GET /api/me` photos add `original: "downloaded" | "buy on site: …" | "failed: …" | null`.
- `POST /api/originals {profile_id}` → status (400 non-yipai / no marked photos, 404 profile, 409 running). `GET /api/originals` · `POST /api/originals/cancel` → status `{state: idle|running|done|cancelled|error, profile_id, profile, done, total, current (fname), counts: {downloaded, skipped, buy_on_site, failed}, errors: ["<source id> <fname>: <status>" …, last = job error], folder}`.
- `GET /api/originals/zip?profile_id=` → attachment `<collection>-<safe profile>-originals.zip` (404 none, 400 non-yipai). `POST /api/photos/{id}/original {profile_id}` → JPEG attachment (402 buy on site, 409 job running, 502 API/failed, 400 non-yipai).
- `POST /api/export {profile_id}` → `{path: ".../exports/<coll>/<safe>/photos.csv", count}`; columns `source_photo_id, original_file_name, photographer, taken_at, album, preview_path, original_path, status`.
- Profile create/rename: new 400 on folder collision. Rename/delete → 409 for the profile being downloaded; rename of another profile → 409 only while a download runs and its folder must move.

**Evidence**
- `uv run pytest -q`: 233 passed (+18 `tests/test_originals.py`; 3 export tests in `test_web.py` rewritten for the CSV). All HTTP via MockTransport; writes under tmp_path.
- Mutations 15/15 caught (photoId match, paging, page cap, 403/non-JPEG classification, skip-existing, valid w/o JPEG check, `.part` treated as done, cancel check in pause, 409 guard, running-profile rename/delete guard, safe-name regex, lookup pacing, collision check, rename move); restored from copies, sha256-verified.

### S1-T2 fix round
- Rename that moves a folder rewrites `photos.csv` via `write_csv(new, marked rows)` while holding `busy`, so `original_path` (and the zip's CSV) point at the new folder; if the rewrite fails the folder is moved back and the DB update rolls back.
- `Job.cancel` does check-and-set under `Job.lock`; `claim()` clears `stop` and applies the new state under the same lock, so a late cancel can't reach a newer single-photo request or batch. `/api/photos/{id}/original` also maps `Cancelled` → 409 `"download was cancelled; try again"` (never a 500).
- Tests: `test_rename_after_download_rewrites_csv_paths`, `test_cancel_signal_during_single_photo_is_409_not_500`, `test_cancel_checks_and_sets_stop_atomically_with_job_state` (Event subclass asserts `Job.lock` is held when `stop.set()` runs); `test_cancel_interrupts_pacing_wait_and_blocks_other_downloads` now also renames another profile that has a folder while the job holds `busy` → 409, nothing moved, name unchanged.
- Evidence: `uv run pytest -q` 236 passed; reverting each fix (no CSV rewrite, Cancelled uncaught, pre-fix unlocked cancel, rename claim bypassed) → 4/4 caught, restored + sha256-verified.

**T2 review (orchestrator)** — fresh reviewer: 0 CRITICAL / 0 MAJOR, 5 MINOR. Routed and fixed (re-check: all CLOSED, 236 passed): stale CSV paths after rename, late cancel hitting the next job / 500, untested rename claim → 409. Rejected: `statuses` uses `is_file` not `valid` (files only land via `write_atomic`; validating every original per `/api/me` costs a full read each); `Thread.start` failure leaking `busy` (speculative). Deferred (also seen): zip temp orphaned if the build raises; `valid()` reads every original on rerun; `looks_like_jpeg` EOI heuristic on padded JPEGs (inherited policy); "not in the gallery manifest" maps to 502 on single photo.
- **Real originals run** (orchestrator, pre-fix-round T2 code, port 8001 on `data/subsets/fullcopy`): `POST /api/originals {profile_id: 1}` → done 10/10 downloaded, 0 errors, ~1 lookup/s. Each file is a JPEG with exactly the manifest width×height (6000×4002 / 4002×6000 Canon R5m2, 4608×3072 A7M4, 5100×3400 A7M5), EXIF with camera + DateTimeOriginal; names `20260926-120000_示例摄影工作室_10000000.jpg` …; `photos.csv` has the BOM, the 8 columns, chronological rows, all `downloaded`. Bytes are ~88–93% of manifest `size`: the `sign` URL carries `x-oss-process=image/watermark,…,g_sw` — the organizer's FUGA branding band along the bottom, the same bytes the site's 下载 button gives (spec fact). No unbranded source was probed (private bucket; out of scope).
