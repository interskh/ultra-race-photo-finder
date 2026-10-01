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
