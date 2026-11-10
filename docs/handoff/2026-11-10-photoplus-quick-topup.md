# 2026-11-10 photoplus quick top-up

## T1 - quick check + listing snapshots
**Decisions**
- Snapshots live in manifest table `listing(list_key,pos,source_id)` (created by `Adapter.attach`, not base SCHEMA, so other platforms are untouched); counts in meta `count:<list_key>`; set of sub-album keys in meta `listing:albums`.
- `listing:albums` doubles as the "sealed/complete" marker: deleted when a full listing starts, written last after `list` finishes. Interrupted run => no marker => next run full. Each listing is staged in memory by page (relist-safe) and swapped in one transaction when read to its end.
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
