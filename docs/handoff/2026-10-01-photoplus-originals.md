# T1 - photoplus originals backend

## Decisions
- `photoplus.Locator` (in sources/photoplus.py): page 1 for the total, then bisect pages on `relate_time` DESC; in-range page scanned for the id, ties scanned outward while the page edge time equals the target. Raises `photoplus.NotFound` -> `Failed` ("failed: not found in the photoplus album", "no shot time recorded").
- Budget 24 network listing requests per lookup (cache hits free); exceeding it = NotFound "kept changing". Page cache TTL 240 s keyed (activity, page); 2.5 s gap between listings via `Fetcher.pause` (Cancel interrupts).
- 403 -> `locator.forget` drops only the page the id was found on (bisection pages stay cached), relocate once, then `buy on site: HTTP 403`.
- `Fetcher.download(urls, headers)` is the shared retry loop (yipai `fetch` now calls it; behaviour unchanged). `Forbidden(BuyOnSite)` marks 403.
- Rows gain `shot_at` (manifest taken_at, not the index's) and `site_id`; `read_manifest` tuples are now (fname, order, shot).
- `originals.Unavailable("<platform> API unavailable: ...")` replaces catching `yipai.Blocked` in Job.run and app.py; yipai text unchanged.
- `file_name(meta, platform)`: photoplus gets `photoplus-<id>`; yipai/others unchanged. `is_yipai` -> `has_originals` (yipai manifest OR `albums/photoplus-*`), RaceState field `has_originals`, facet key `originals` unchanged.

## Rejected
- Per-photo lookup: photoplus has none. Refetching all pages on 403: ~10 requests x 2.5 s per expiry. Reusing `yipai` fetch with `if platform`: Referer/URL shape differ.

## Assumptions
- Signed URLs live > a few minutes (measured 4 min); 403 handling covers shorter. Check: E2E on the rehearsal root.
- `relate_time` == manifest `taken_at` string for every photo (checked on siguniang by the spec's probes).

## Deferred
- UI gate/wording, README, ROADMAP, CLAUDE.md (T2). No real-site E2E here.

## Touches
- src/photofinder/{originals.py,sources/photoplus.py,web/app.py}; tests/{test_photoplus_originals.py (new),test_site_links.py,test_race_scan.py}. Public: 400 text for no originals, 409 open-on-site text, `originals.has_originals`, `originals.Unavailable`.

## T1 review round 1
- Decisions: `locate` counts cached-page uses; a NotFound after using any cached page drops that activity's cache and retries once fully fresh (shares the 24-request budget). This also covers a stale `pics_total` hiding the last pages, without a fresh page 1 per lookup (which would cost a request per photo).
- Missing `pics_total` is now `NotFound("photoplus did not report the album size")` instead of collapsing to page 1.
- Second photoplus 403 -> `failed: photoplus refused the download link again after relisting (HTTP 403)`; yipai 403 still `buy on site`.
- Budget message: "photoplus listing request limit reached while locating the photo; try again later" (`BudgetExceeded(NotFound)`, no fresh retry).
- Tests: shifting-album regression (cached p3 / p1 + newer upload; found by brute force on the old code), absent id retried fresh once, missing total. Mutations 3/3 caught (retry gate, total fallback, 403 text); the 4th (dropping the BudgetExceeded short-circuit) is behaviourally equivalent.

# T2 - UI gate + docs

## Decisions
- Viewer gate `#m-acts` allows platform yipai, photoplus or null; `origTag` open-on-site title names yipai360 and photoplus (still shown only for pailixiang/xxpie).
- README/ROADMAP/CLAUDE.md: originals = yipai360 + photoplus; photoplus watermark and ~30 s cold lookup noted; test count 608 (full suite 2026-10-01).

## Rejected
- JS test for the gate: no JS harness exists; the gate is one expression, covered by reading plus test_web.

## Assumptions
- The ~30 s figure is the spec's estimate (about 10 listings x 2.5 s), not re-measured.

## Deferred
- Real-site E2E of the viewer button.

## Touches
- src/photofinder/web/static/index.html (2 lines), README.md, docs/ROADMAP.md, CLAUDE.md.

## Gate fixes
- Real photoplus bodies are complete but carry trailing data after the last FFD9, so `looks_like_jpeg` rejected all of them. Photoplus now uses `photoplus_complete`: FFD8, >1024 bytes, and length == the `:<bytes>.jpg` size declared in the URL path when present; a mismatch is retried as "truncated download". yipai check unchanged (`Fetcher.download(complete=, truncated=)`).
- `valid(dest, platform)`: photoplus rows accept FFD8 + >1024 bytes (files are written atomically), so reruns and single downloads skip existing files.
- Tests: real-shaped body (FFD9 mid-file + 500 trailing bytes) downloads, reruns/single with zero requests; size mismatch retried then `failed: truncated download`. Mutations 3/3 caught (default check, skip check, size comparison).
- README originals paragraph: yipai360 6 s lookup vs photoplus 2.5 s listing, `…_photoplus-<id>.jpg`, refusal after relist is `failed: …`, "tens of seconds" instead of ~30 s. Touches: originals.py, README.md, tests/test_photoplus_originals.py.
- Codex round: CSV statuses are keyed (source id, file name, `site_url`); an old CSV without a `site_url` column still matches on (id, name) only. Collision test restored for yipai+photoplus.
- Budget now counts every HTTP attempt (incremented in the `fetch_json` `fresh` callback, so retries spend it); retry sleeps are `max(backoff, gap)` through `Fetcher.pause`.
- The fresh retry after a cache-assisted NotFound has its own budget: worst case is 2x `MAX_PAGE_REQUESTS` (48) HTTP attempts per lookup.
- Tests fail without each fix (4/4 reverts caught): attempt-counted budget, retry gap, own fresh budget, CSV collision.
- Real-site E2E: live URLs end `:<bytes>.JPG` (uppercase); the declared-size match is now extension-agnostic/case-insensitive and fixtures use `.JPG`. Downloaded files equalled the declared sizes exactly (7220286, 4888854, 3864617 bytes); the mismatch test fails with the old lowercase-only regex.
