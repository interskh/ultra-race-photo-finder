# Nearby shots — handoff

## T1 — backend + eval (sequence logic, /neighbors, /nearby, eval stats)

**Decisions**
- New module `nearby.py`, not `search.py`: search.py is scoring over all persons; this is roll/sequence + dedupe logic over a handful of photos. It reuses `search.WEIGHTS`/`filter_where`.
- Roll = one query for every needed `photographer_uid` (`status='ok'`, `taken_ts` not null), ordered `photographer_uid, taken_ts, length(source_photo_id), source_photo_id, id`, grouped in Python. 300 random gongga anchors at span 5: 57 ms (read-only check).
- Best match = raw weighted cosine (osnet 0.3 / siglip 0.7, `nan_to_num`, like `matched_via`), max over (neighbour person × reference person); no z-score (2–3 persons is noise). Persons labelled not_me never win the match, in both endpoints.
- `/neighbors` keeps every photo of the roll (raw filmstrip); a neighbour with no eligible embedded person gets `person_id: null`. `confirmed` = a reference person is Me-marked by this profile. `person_id` overrides the Me reference; must be in the photo (400) and embedded (400).
- `/nearby` anchors = photos with a Me person of this profile + photos with a person whose bib text == `anchors_bib` exactly (that person is the reference, unless the profile marked it not_me). Anchors are not filtered (a shot just inside the time range next to one just outside is still valid); the neighbour cards are.
- `anchors_bib` on a race with missing/partial OCR warns (same text as start_bib) instead of failing: Me anchors don't need OCR.
- Excluded from `/nearby`: every anchor photo (so every photo with a Me person, even one whose Me person has no embedding).
- Skip rule: a neighbour with no eligible person is dropped if it has a not_me person or a bib filter is set; otherwise kept with `person_id: null` (no person detected / not embedded). Bib filter keeps only persons matching it, so zero-person photos drop under a bib filter.
- Dedupe key per neighbour photo: (|offset|, |gap|, anchor photo id), smallest wins. Sort: |offset|, taken_ts, photo id.
- Cards carry the same keys as `/search` results (`rank`/`score`/`matched_via` null, `box` null when no person) plus `offset, gap_s, same_second, similarity` (+ `anchor_photo_id, anchor_person_id` on /nearby; anchor_person_id falls back to the anchor's first reference when the neighbour has no person).
- **For T2 — dedupe with /search: new `SearchQuery.exclude_photos`.** It is unioned into the exclude set but NOT into the rank base, so similarity ranks stay 1.. and Load more stays right. Fetch `/nearby` once per query, pass its photo ids as `exclude_photos` on every `/search` page (first page and Load more, alongside `seen`). Not applied to `start_bib` searches: those results are exact bib hits = anchors, which `/nearby` already excludes, so they cannot overlap.
- **For T2 — `tests/test_web.py::test_page_is_served_and_calls_only_real_endpoints`** now allows exactly the two new routes to be unused by the page; T2 must shrink that set back to `used == routes`.
- Eval: per bib, anchors = all bib photos; precision is per (anchor, neighbour ≤ span) pair; gain = R@50 of the default 0.3:0.7 ranking vs R@50 of (top-50 ∪ neighbours of the query photo). Printed as "lower bounds" because bib reads are the only truth.

**Rejected**
- Passing nearby ids via `seen`: `ranked` adds `len(seen)` to the rank base, so the first similarity card would show rank N+1.
- Filtering anchors by the search filters: would drop valid shots at range edges; filters belong to what is shown.
- Ordering by `source_photo_id` text alone: "100" < "99"; also by `id` alone: scan order is not shot order.
- Per-anchor roll queries: one query per photographer suffices and stays O(photos of those photographers).
- Widening eval `Row`: separate `NearRow` keeps the existing table and its tests untouched.

**Assumptions**
- `taken_ts` is second precision and `photographer_uid` identifies one camera roll on all platforms (check: `select photographer_uid, count(distinct camera) from photos group by 1`).
- Within one uid the source id format is constant (numeric or hex), so length-then-text gives numeric order for numeric ids and a stable order for hex.
- Anchor count stays below SQLite's variable limit (32766) for `in (...)` lists.

**Deferred**
- UI (T2). README/ROADMAP/CLAUDE.md layout + test count (563 passed + 1 skipped now): left to the docs step so T2's routes land with them.
- No paging on `/nearby` (bounded by anchors × 2 × span ≤ 10 per anchor).

**Touches**
- `src/photofinder/nearby.py` (new), `src/photofinder/web/app.py` (`SearchQuery.exclude_photos`, `NearbyQuery`, `shot_cards`, two routes), `src/photofinder/evaluate.py` (`NearRow`, `nearby_bib`, `mean_near`), `src/photofinder/cli.py` (`print_near` in `eval`), `tests/test_nearby.py` (new), `tests/test_web.py` (page-routes test).
- Public API: `GET /api/r/{slug}/photos/{id}/neighbors`, `POST /api/r/{slug}/nearby`, `exclude_photos` on `/search`.
