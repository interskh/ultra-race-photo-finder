# T1 - pailixiang + xxpie originals backend

## Decisions
- `pailixiang.Locator` / `xxpie.Locator` (subclass `common.Paced`: >=2.5 s between requests per platform via `Fetcher.pause`, retry sleeps `max(wait, gap)`); one `Adapter` per album reused (pailixiang: one `AlbumGetView` per album, cached). Paging bounded by `MAX_LOOKUP_PAGES=5` when a page is full. `NotFound` moved to `sources.common` (still importable as `photoplus.NotFound`).
- `Adapter` gained `headers=` (merged into API requests; shared Fetcher client has no default headers); pailixiang search body factored into `Adapter.search(start, **extra)`, used by `list_page` too.
- pailixiang image GET uses `IMAGE_HEADERS` (HEADERS minus Content-Type); completeness = `looks_like_jpeg` + len == FileSize1 when int. xxpie reuses the declared-size check (`photoplus_complete` renamed `declared_complete`, same behaviour; `valid()` uses it for photoplus+xxpie, pailixiang falls to `looks_like_jpeg`).
- `Fetcher.listed_original` replaces `photoplus_original`: lookup, 403 -> relookup once -> `failed: <platform> refused the download link again after relisting (HTTP 403)`. `locator.forget` only for photoplus.
- Outage isolation in `Job.attempt`: `Unavailable.platform`; on outage rows of that platform fail with `failed: <P> API unavailable: ...` and no requests; job ends `error` with all outage messages in `errors`. If no remaining row belongs to a non-down platform the job stops at the outage exactly as before (keeps `done` count and the old yipai/photoplus tests unchanged).
- `file_name` prefix `<platform>-` for photoplus/pailixiang/xxpie; `ORIGINAL_PLATFORMS` = all four; `has_originals` still scans those four platform globs (false for a race with only unknown-platform manifests).
- app.py texts: 400 "yipai360, photoplus, pailixiang and xxpie"; 409 "originals cannot be downloaded for this photo; open it on <platform> instead".

## Rejected
- Always-continue-after-outage (would change `done` and the old single-platform tests); a `glob("*/manifest.sqlite")` has_originals (would count unknown platforms).
- Caching lookups per name for pailixiang/xxpie: one request per photo is already the spec's cost.

## Assumptions
- pailixiang `site_id` carries the leading `a` (Adapter strips it); xxpie `url_origin` path ends `:<bytes>.<ext>`. Check: E2E on rehearsal data.
- Signed URL lifetimes for xxpie unknown; 403 relookup covers it.

## Deferred
- T2 (index.html gate/origTag wording, README, ROADMAP, CLAUDE.md test count); real-site E2E.

## Touches
- src/photofinder/{originals.py,web/app.py,sources/{common,pailixiang,xxpie,photoplus}.py}; tests/{test_plx_xxpie_originals.py (new),test_photoplus_originals.py,test_site_links.py,test_race_scan.py}.
- Existing tests edited (contract change: pailixiang/xxpie no longer "open on site"): they now use an unknown platform ("elsewhere") or monkeypatch `ORIGINAL_PLATFORMS` as the non-downloadable stand-in; the photoplus 409-text assertion. Yipai Blocked/Unavailable job tests in test_originals.py and the photoplus outage test passed unchanged.

## T1 review round 1
- xxpie registration is paced: `Adapter.registrar` hook; `Locator` sets it so a (re-)register waits for the gap before and sleeps a gap after (search never follows it back to back). At most 2 registrations per lookup (first + one renewal), then ValueError -> retries exhaust -> `xxpie API unavailable`. `Adapter.ok` (clears token on any non-zero code) unchanged.
- Early stop with 2+ down platforms: every remaining row of a down platform (incl. the current one) gets `failed: <P> API unavailable ...` in the CSV; done/counts unchanged. With exactly one down platform the stop is as before (rest of the CSV statuses untouched) because `test_api_failure_stops_job_with_error` asserts empty statuses there - kept unchanged.
- Tests (first-failing): pacing order/gaps, <=2 registrations, two-platform stop marks all rows; cancel-in-gap xxpie test now signals on registration. Mutations 3/3 caught (rows[i:] slice, registration bound, post-register gap).

# T2 - UI + docs

## Decisions
- Viewer gate: allow the four `ORIGINAL_PLATFORMS` explicitly (null platform still allowed); hide only for unknown platforms. origTag 'open on site' title no longer names platforms.
- Docs state pailixiang copy has no EXIF, photoplus/xxpie keep it; E2E numbers written as given (pre-review-fix code), orchestrator to update.
- CLAUDE.md test count 638 passed (fresh `uv run --frozen pytest -q`, 1 skipped).

## Rejected
- Reading the platform list from the backend facets: extra API surface for a 4-item list.

## Assumptions
- Unknown platforms still render `open on site`; the old photoplus ROADMAP line (line 15) kept as history with a pointer.

## Deferred
- Final E2E numbers (orchestrator).

## Touches
- src/photofinder/web/static/index.html, README.md, docs/ROADMAP.md, CLAUDE.md.

## Gate fixes (whole-run round 1)
- A (contract change, orchestrator-approved): the early stop is gone. After `Unavailable` from P, every later row of P gets `failed: <P> API unavailable: ...` with no request, other platforms continue, job ends `error`, done == total, final `errors` entry is the outage message. `Job.attempt(row, dest, down)` lost its `rest` arg; `Job.run` no longer catches `Unavailable`. Tests updated to this contract: `test_originals.py::test_api_failure_stops_job_with_error` (done == total == 2, both CSV rows `failed: yipai360 API unavailable...`, counts failed 2) and `test_photoplus_originals.py::test_api_blocked_names_photoplus...` (done == total). The `len(down) > 1` special case and its test were removed.
- B: down-platform rows still pass the rerun skip check first (valid file -> `skipped`/downloaded); an invalid existing file is unlinked before the row is marked failed (so the final CSV rewrite cannot report it downloaded).
- C: exhausting `MAX_LOOKUP_PAGES` full pages -> `failed: lookup limit reached after 5 pages of results for <fname>` (pailixiang + xxpie); a short page without a match stays `not found in the <platform> album`. The old pailixiang test expecting not-found was fixed.
- D: README originals paragraph scoped to yipai360 EXIF; photoplus/xxpie keep EXIF, pailixiang copy has none.
- Tests: order-independent outage (4 orders), two platforms down, skip-valid/drop-invalid after outage, limit message for both platforms. Mutations 5/5 caught (unlink, skip order, early raise, 2x limit message).

## E2E (real sites, 2026-10-01)
- Setup: rehearsal root `data/rehearsal-plx-xxpie` (PHOTOFINDER_DATA_ROOT) with `.backup` copies of the 贡嘎 and Chongli indexes and every album manifest, photo dirs symlinked; one server (the user's stopped and restored after); exports written only under the rehearsal root. Rehearsal shortcut: no Me marks existed on pailixiang/xxpie photos, so profile 1 in the COPIES got 4 pailixiang, 4 xxpie and 2 photoplus person labels by SQL (the feature under test is the download path, not marking).
- At 3794b6c-pre-review code (T1 uncommitted): 贡嘎 My photos → Download originals: 30/30 (26 yipai + 4 pailixiang), 0 failed, ~3 min; pailixiang 1 AlbumGetView + 4 searches + 4 image GETs, all 200; files equal the catalog's uploaded size (2000×1125 … 6000×4000). Chongli: 6/6 (4 xxpie + 2 photoplus), ~2 min; xxpie 1 register + 4 searches + 4 images; xxpie files 2656–5078 px with EXIF.
- At 64a18ff (final code, server restarted): deleted one pailixiang and one xxpie original in the rehearsal folder; viewer → Download original re-fetched each (pailixiang: album view, search, image; xxpie: register, ~2.7 s gap, search, image; 5.4 s), 200. Rerun of both jobs: 6/6 and 30/30 skipped, zero outbound requests. Screenshot `data/exports/screens/plx-xxpie-originals-myphotos-20261001.png` (cards show "✓ original", "30 / 30 · 30 already there"). The 贡嘎 rerun was started via the same POST the button sends (browser automation lost the race switch); the Chongli rerun and both viewer downloads went through the UI.
- Not exercised on the real sites: 403 relookup, API outages, search-limit paging (covered by tests).
