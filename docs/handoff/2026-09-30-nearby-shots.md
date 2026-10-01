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
- **For T2 — dedupe with /search: new `SearchQuery.exclude_photos`.** It is unioned into the exclude set but NOT into the rank base, so similarity ranks stay 1.. and Load more stays right. Fetch `/nearby` once per query, pass its photo ids as `exclude_photos` on every `/search` page (first page and Load more, alongside `seen`). Not applied to `start_bib` searches: `/nearby` with `anchors_bib` excludes every photo with a person whose bib == anchors_bib (corrected in Gate fixes: a bib person marked Not me is not an anchor, but its photo is still a bib result, so the two could overlap before).
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

## T2 — UI (viewer filmstrip, "Next to your marked photos" section, docs)

**Decisions**
- Strip under the stage (`.m-main` column, fixed `--strip: 144px`, image max-height shrinks only with `.has-strip`): the 352 px side panel can't hold 7 landscape thumbs.
- Modal keeps `list`/`i` = the result the user opened from (Prev/Next, `n of N`, close refocus); a walked shot is a third arg `shot` and `m.r` is the displayed photo (download original uses `m.r`). Stale guard is modal identity (`S.modal === m`).
- Confirmed = `S.mine.has(photo)` (client truth, no wait for details) or opened from bib-start results → `person_id` = the hit person. The response's `confirmed` is not used (false for bib hits).
- Strip edge: refetch around the current photo only if it has a Me person; at the anchor's own edge the roll has ended. Hints are rendered in the strip head, not banners. Errors (e.g. 400 unembedded bib person, `reason`) show inline in the strip.
- Marking Me on the displayed photo (re)anchors there; unmarking hides only when the anchor has no Me left and it isn't a bib anchor (or its bib person is now Not me).
- Shots and nearby cards: best match pre-selected cyan + "likely same runner" (box tag, list tag, legend); no yellow "matched" hit for them.
- Prev/Next: nearby cards and ranked cards are separate lists (each grid passes its own array to `card()`): `n of N` stays meaningful, a section refetch can't shift ranked indices, and card-keyboard nav already works per grid.
- Badge click: Me anchor → `showMatched` (same as matched via); bib anchor (not Me) → opens the anchor in the viewer, because My photos would say "no longer marked".
- Find more: `/nearby` awaited before `/search` (ids feed `exclude_photos`); bib: both in parallel, no `exclude_photos` (T1). Load more reuses the current nearby ids.
- Section controls refetch the section only (`S.nseq`), dropping nearby cards already in the ranked grid. Prefs in one key `photofinder.nearby` `{on, span}`.
- Section shown when Find more (Me ≥ 1) or bib start with Me marks or bib hits; empty → hint; toggle off → header + "Off" hint only.

**Rejected**
- One combined Prev/Next list (nearby + ranked): rebuilding it on every Load more/refetch, and positions shifting under the user.
- Re-running page 1 of `/search` on toggle/stepper change: spec says section only; the cost is that photos dropped from the section (span shrunk / toggle off) stay out of the ranked list until Load more or the next search.
- Auto-re-anchoring when merely walking onto an already-marked shot: the strip would jump each step through a marked burst; it re-anchors on the edge step or on a new mark instead.
- Painting strip cells via `paint()` / `data-pid`: cells reflect the photo (any Me person = me) and are re-rendered after each mark.

**Assumptions**
- `/neighbors` neighbours come ordered by offset (they do: `rolls` iterates j ascending).
- `gap_s` is whole seconds (second-precision `taken_ts`).

**Deferred**
- No automated JS behaviour test in the repo (no JS runtime in the test stack); a jsdom smoke harness with canned API responses lived in the session scratchpad (32 checks; mutants: pytest page tests 3/6, harness 6/6). Real-browser E2E is the orchestrator's.
- Edge-walk past a bib hit that isn't Me does not refetch (only Me counts as confirmed while walking).
- Nearby section isn't refreshed when you mark Me on ranked cards (would reshuffle under the user); the next Find more picks the new anchors up.

**Touches**
- `src/photofinder/web/static/index.html` (CSS, `#near` section, `#m-strip`, `.m-main` wrapper, search/openModal/closeModal/onKey/setLabel/card).
- `tests/test_web.py` (route test back to `used == routes`; new page test), `README.md`, `docs/ROADMAP.md`, `CLAUDE.md` (layout line, 564 passed).

## Gate fixes (round 1)

**Decisions**
- `/nearby` with `anchors_bib` hides every photo that has a person with that exact bib, labelled or not (`nearby.collect` `hidden`): those are the bib results, so the section can't repeat them. Regression: `test_nearby_never_lists_a_bib_result_photo_even_when_its_bib_person_is_not_me`.
- Once-only on the page, client side too: every `/search` page drops photos currently in the nearby section; `refreshNear` drops photos already ranked. Rank numbers may skip (accepted). Bib paging now counts raw rows (`S.got`) so a dropped row doesn't shift `offset` or `done`.
- A `/nearby` response is used only if `S.nseq` is unchanged and the toggle is still on (a toggle/stepper change bumps `S.nseq`).
- Bib-start result whose person is Not me (current `S.labels`) is not confirmed: no strip.
- Losing the anchor's last Me re-evaluates the anchor wherever you are: re-anchor on the current photo if it has a Me person, else hide. Works when the label response lands after you walked away.
- Strip renders only once it has neighbours (or an inline error): no strip and no image shrink for photos with `reason` (no time/photographer), single-shot rolls, or while the first fetch loads.
- Badge always shows the gap (`2 min before`), never `next shot`.

**Rejected**
- Hiding a neighbour photo that has one Not me person and another unlabelled one (coordinator: /search drops persons, not photos).
- Showing "Loading shots…" in the strip on first open: it shrank the image for photos that end up with no strip.

**Touches**
- `src/photofinder/nearby.py` (`collect`), `tests/test_nearby.py`, `src/photofinder/web/static/index.html` (`search`, `renderStrip`, `modalLabelled`, `openModal`, `nearText`). Scratchpad jsdom harness: 38 checks; 6 new ones (fixes 2–7) failed before the fix.
