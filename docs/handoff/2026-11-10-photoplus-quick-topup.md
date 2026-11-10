# 2026-11-10 photoplus quick top-up

## T1 - quick check + listing snapshots
**Decisions**
- Snapshots live in manifest table `listing(list_key,pos,source_id)` (created by `Adapter.attach`, not base SCHEMA, so other platforms are untouched); counts in meta `count:<list_key>`; set of sub-album keys in meta `listing:albums`.
- `listing:albums` doubles as the "sealed/complete" marker: deleted when a full listing starts, written last after `list` finishes. Interrupted run => no marker => next run full. Each listing is staged in memory by page and swapped in one transaction when read to its end.
- Seal is skipped if any count (album `pic_num`, list `pics_total` on page 1) is not a plain int, so such albums are always full-listed.
- Quick check = reuse the cached `/album/albums` + `/pic/list` page 1; on change that page-1 request is repeated by the full listing (1 wasted request, no caching code).
- `/pic/list` now always reads to ceil(pics_total/100) pages (dropped the `seen == total` early stop); `seen > total` warning + short-page paging unchanged.
- Hook: `AlbumDownloader.run(full=False)` calls `adapter.attach(db, full)` if present; `download_album(..., full)` passes it only to non-yipai.
- `scripts/download.sh` strips `-` args before `select_albums`.
**Rejected**
- Table in base SCHEMA (touches every platform). Per-run snapshot id/generation column (more state; marker-delete is enough). Caching the page-1 reply for the full fallback (T2 replaces fallback anyway).
**Assumptions**
- Sub-album `pic_num` equals what `/album/one` lists; if it does not, that album is simply re-listed every run (count mismatch is stored as reported, so compare is stable only when equal - check on a live album).
- Count equality means "unchanged" (an add+delete pair is missed until `--full`; T2 / `--full` cover it).
**Deferred**
- Bisect of changed listings (T2). Snapshot ids include unusable/skipped ids exactly as listed.
**Touches**
- sources/photoplus.py, sources/base.py (`run(full)`), cli.py (`--full`, `download_album`), scripts/download.sh, tests/test_photoplus.py, docs/usage.md. Manifest meta keys `count:*`, `listing:albums` and table `listing` are new shared surface for T2.

## T2 - bisect changed listings against their snapshots
**Decisions**
- Mode `diff` in `Adapter.list_page` (plan() replaced unchanged()): changed listings in order (sub-albums, then `list`), driven by generator `bisect_listing(old_ids, m, page_size)` that yields page numbers and gets id lists back; one remote request per `list_page` call (the cached plan-time `/pic/list` page 1 is consumed once, free).
- Gap test uses unread slots: anchors found by scanning the contiguous fetched run left/right of an unread region (virtual anchors -1 / n at the ends), resolved when `len(S between anchors) == unread_pages * page_size`. The brief's `r_b-r_a == s_b-s_a` is wrong when the fetched pages have non-anchor (new) items next to the region; this form handles all-new pages at the top/middle too (measured: 5 adds mid-list of 3000 = 3 `/pic/list` reads incl. plan page 1).
- Validation: length == count and no duplicate; else linear read of that listing (refetched, stops at a short page, max npages+1 pages). Stored count is the count seen at plan time, so uploads during a run self-heal on the next run.
- New rows are inserted into the catalog (pending, `insert or ignore`) inside `list_page` before the snapshot is stored, so a crash before the downloader's upsert leaves a non-done row and the next run lists everything.
- Relist (same cursor, incl. first call's `None`): refetches the pages the previous call consumed, re-emits only the previously emitted ids with fresh URLs, does not touch bisect state.
- `gone(source_id, noticed_at)` created in `attach`; `store("list")` records old-minus-new ids (also on full runs, so `--full` keeps it honest) and clears reappeared ones; sub-album stores never touch it. Seal rewritten at the end of every diff run (drops removed sub-albums).
- Diff mode returns `total=None` (no false "missing" warning; skipped ids never seen would skew it).
**Rejected**
- Relying on r/s index equality (see above). Storing snapshots when the diff validates but before rows reach the catalog (crash window). Reusing fetched pages for the linear fallback (the inconsistency may be a mid-run upload). Clearing `gone` from sub-album pages (list is authoritative).
**Assumptions**
- Page order is stable between requests (validated, not trusted); live `/album/one` page 200 and `/pic/list` page 100 as stated in the brief; not run against the live site.
**Deferred**
- Add+delete that cancel inside one unread span (or an unchanged-count listing) stay invisible until `--full` (documented in ROADMAP). No-anchor virtual ends assume the fetched run reaches page 1 / last page.
**Touches**
- sources/photoplus.py only (new table `gone`, helper functions `anchor`, `bisect_pages`, `bisect_listing`); tests/test_photoplus.py (FakePP `expire_next`; five T1 fallback tests rewritten); docs/usage.md, docs/ROADMAP.md, CLAUDE.md (layout word, test count 761).

## Gate fix 1
**Decisions**
- T1 entry corrected: the "relist-safe" claim was wrong. `finish()` popped the staged pages, so a 403 relist of a listing's last page re-stored a one-page snapshot, recorded the rest as `gone` and sealed. Now `finish()` keeps staged pages (a relist overwrites its page, the re-store is idempotent).
- Full mode reserves the finishing page's rows (`insert or ignore`, pending) before `finish`/`seal`, same as diff mode (`Adapter.reserve`).
- Linear fallback is validated (length == count, no duplicates); invalid => not stored, `unsealed` flag skips the seal, warning logged; the old count stays so the next run diffs again. An empty `list` against a non-empty snapshot is ignored the same way (guard in `store`, also covers `--full`).
- Pacing lives in `Adapter.get` (sleep `PAGE_GAP` since the last request via the injected sleep/clock), so every request, including count checks and relists, is spaced; no restructuring into one request per call. The test `site` fixture sets `PAGE_GAP=0` for CLI tests that use the real sleep.
- Quick path: `bad_files()` scandirs `photos/` once and compares sizes with the done rows (missing or <= 1 KB => full). `attach(db, full, photos_dir)`; `base.run` passes `photos_dir`. 60k synthetic files: 0.23 s. A truncated JPEG above 1 KB is not caught.
- Ids missing from a listing get a unique snapshot placeholder `!none:<key>:<page>:<i>`; never an anchor, never `gone`.
- `scripts/download.sh`: race = first non-dash argument.
**Rejected**
- Reading `photos_dir` from `pragma database_list` (hacky). Full `jpeg_file_ok` per file (too slow at 60k). Keeping one request per call by splitting plan into its own call (more state than a gap sleep).
**Assumptions**
- Pacing on the injected clock (time.time) plus the downloader's own page delay is enough; the first changed call now takes about 7.5 s of gap sleeps once.
**Deferred**
- `test_a_crashed_diff_page_leaves_its_new_photos_in_the_catalog` only fails on HEAD through the new `attach` signature (diff mode already reserved rows).
**Touches**
- sources/photoplus.py, sources/base.py (`attach` gets `photos_dir`), scripts/download.sh, tests/test_photoplus.py, tests/test_download.py, docs/usage.md, docs/ROADMAP.md, CLAUDE.md.

## E2E (orchestrator, 2026-11-10)
- Real race, 4 photoplus albums (~60k photos, 24 sub-album listings + 4 `list` listings), run from this worktree with `PHOTOFINDER_DATA_ROOT` on the main data root.
- Seed run (no snapshots yet): every album logged "no complete listing snapshot yet; listing everything"; 4248 s; 0 new photos; 0 `gone`.
- Every stored snapshot length equals its reported count (`pic_num` per sub-album, `pics_total` for `list`), 28/28 listings: the T1 assumption that `pic_num` equals what `/album/one` lists holds on these albums.
- Quick run right after: every album logged "unchanged since last check"; 21 s for all 4 albums (vs about 1 h before).
- Not exercised live: the bisect path (no album changed between the two runs). Covered by the fake-server tests only.
