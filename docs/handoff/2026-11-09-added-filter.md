
## Task 1: backend for the "Added" (top-up batch) filter

**Decisions**
- Batch = maximal run of `scanned_at` values (status ok, non-null, sorted) with consecutive gaps <= 600 s (`search.BATCH_GAP_S`). A real scan spans seconds to ~30 s and crosses minute boundaries; separate top-ups are hours apart, so 10 min separates them with wide margin.
- Query value encoding `"<start>..<end>"` (`YYYY-MM-DD HH:MM:SS`, inclusive); the UI echoes the facet bounds. Server never re-clusters at query time, so a stale facet in an open tab still filters correctly.
- Facet computed live in `facets_of` (one ordered scan of scanned_at, python loop); no stored state.
- `added_range` in app.py validates with a strptime round-trip (rejects unpadded `2026-11-1 1:00:00`), start <= end, 400 `added ...`.
- Filters.added is trailing/defaulted (cli.py builds Filters positionally); added in `__bool__`. nearby.py unchanged: `filter_where` flows through `dataclasses.replace`.

**Rejected**
- Truncating to minute/hour: splits one scan that crosses a boundary.
- Stored batch id column: needs schema migration plus backfill for existing indexes.
- Server-side re-clustering at query time: a stale facet would silently select different photos.

**Assumptions**
- `scanned_at` is always `YYYY-MM-DD HH:MM:SS` local (stages.py scan); check: `select distinct length(scanned_at) from photos`.
- Cost of facet on ~100k photos is one index-free sorted scan + loop (not measured).
- Re-scan of an existing photo does not rewrite scanned_at (not verified); if it does, a batch would include old photos.

**Deferred**
- UI, docs, CLI flag: task 2 / not requested.

**Touches**
- `search.py` (Filters.added, filter_where, added_batches, BATCH_GAP_S), `web/app.py` (SearchQuery/NearbyQuery.added, added_range, filters_of, facets `added`); tests in test_search.py, test_web.py, test_nearby.py.
- Facet JSON: `"added": [{"start","end","photos"}, ...]` newest first. Query: `"added": ["<start>..<end>", ...]`.
- Decision (fix): `added_batches` groups by `scanned_at` in SQL, then clusters the 16-29 distinct stamps in Python summing counts; per-row strptime cost 0.6-1.2 s on 50-100k photos, grouped version ~30 ms (coordinator-measured).

## Task 2: UI + docs for the "Added" filter

**Decisions**
- Block mirrors Groups (`#f-ad`, checkbox name `ad`, value `start..end` echoed from the facet); wrapped in `#f-ad-block` so label and list hide together.
- Label = `a.start.slice(0, 16)` (`YYYY-MM-DD HH:MM`, same as From/To); server order (newest first) kept.
- Hidden when `added.length < 2`; `renderFacets` toggles it on every load, so an in-page race switch re-shows it. Reset path hides it too.
- Enumeration sites: renderFacets, `filters()`, `countFilters`, `'filtered'` subtitle, `nearBody`, reset list (`f-ad` cleared). The search body spreads `filters()`; Back-history snapshot and Clear iterate all `#filter-form` checkboxes, so they need no change.

**Rejected**
- Showing a single-batch list: filters nothing, adds noise.
- Time-range labels (start-end): longer, wouldn't fit the narrow list.

**Assumptions**
- Facets from an older server lack `added`; `f.added || []` hides the block.

**Deferred**
- No JS test harness (per brief); browser E2E by orchestrator.

**Touches**
- `src/photofinder/web/static/index.html`, `docs/usage.md`.
