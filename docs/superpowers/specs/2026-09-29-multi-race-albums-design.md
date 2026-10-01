# Races and albums across platforms — design

Date: 2026-09-29 · Status: sections 1–2 approved in chat ("sounds good"; UI race picker, lazy per-race load; CLI to add albums; originals for new platforms next phase, but always a way to jump to the photo on the site).

Probe scripts and trimmed API samples were kept in `docs/handoff/2026-09-29-platform-probes/`; that folder is not published (raw site responses). The trimmed samples the tests use are in `tests/fixtures/`.

## 1. Problem & goals

Today one collection = one yipai360 gallery (`data/yipai/<orderId>/`), and `serve` serves exactly that. The user has three races, some with several albums on four platforms:

| Race (display name) | slug | Albums (platform · id · title · photos at probe time) |
|---|---|---|
| 2026 贡嘎100 | `2026-gongga100` | yipai360 · 83415673067642538672 · FUGA 贡嘎100 · 68,488 (indexed) ; pailixiang · a13800138000 · 2026FUGA贡嘎100冰川极境赛 · 715 |
| 2026 Chongli 168 | `2026-chongli168` | xxpie · 65178998a458227944415097 · 八千个瞬间@2026崇礼168 · 3,670 ; photoplus · 39352660 · ACG 2026崇礼168超级越野赛 新闻图 · 6,115 ; photoplus · 89243825 · ACG崇礼168超级越野赛 · 2,156 |
| 2026 四姑娘山 - 云间花径 | `2026-siguniang` | photoplus · 84984877 · …总决赛——精选照 · 8,117 ; photoplus · 30326728 · …总决赛——照片/冲线/个人视频 · 92,889 |

Goals (user-visible):
- Register races and albums from the CLI by pasting the album URL; download each album (paced, resumable, top-up by rerun) into its race; index a race incrementally.
- One search per race spans all its albums (marks, saved people, Find more, My photos, exports are per race).
- The web UI has a race picker; only the selected race's data is loaded; switching unloads the previous race.
- Filters: Album (source album) and Group (yipai tag / photoplus sub-album), photographer across albums.
- Every photo has **Open on site** so the user can buy / download it manually.
- The existing 贡嘎 index (68k photos, 190k persons, the user's marks and three saved people) moves into the race layout without re-indexing and without losing marks.

Evidence of success: all three races downloaded, indexed and searchable from one server via the picker (四姑娘山's 93k album may still be downloading/indexing — operational, not a code gate); 贡嘎 marks identical before/after migration; Open on site lands on the right photo (or album + file-name search) for each platform; full pytest green.

## 2. Non-goals

- Originals download for pailixiang / xxpie / photoplus (next phase; free watermarked full-size files exist on all three — see §Facts). This phase: yipai originals keep working; other platforms show `open on site`.
- Removing watermarks, using paid/no-watermark tokens (`ppSign`, `no_watermark`, `IsNw`), or anything that bypasses a paywall.
- Adding albums or running downloads/indexing from the UI.
- Cross-race search, or saved people shared across races.
- Cross-album duplicate detection (deferred; see §7).
- Recall/model changes.

## Facts established (probes, 2026-09-29; ≤40 paced requests per platform, no login anywhere)

**pailixiang** — API `POST https://mapi.pailixiang.com/plx/<Controller>/<Action>` JSON; needs `Referer`/`Origin: https://live.pailixiang.com` and an `ak` field (static appKey `REMOVED-pailixiang-web-client-key` from the site's `index.js`; 3× pick random digit `e`, append to prefix, `key[e+15]=key[e]`; `ak = prefix + key`) plus `tt:"", ct:0, cv:"169", lang:"cn", pid:"albumview"`. `WapAbm/AlbumGetView {ID:"13800138000", AccessType:"1", ClientType:0}` → `Data.Entity.ID` = internal AlbumID, `Title`. `WapAbm/AlbumSearchPhoto {AlbumID, GroupID:"", SearchType:0, IsPayDownload:false, PhotoSortType:1, IsNw:false, IsEmbed:false, StartIndex:1|81|…, SearchCount:80, SortType:1, OptTime:<echo first response>}` → `{Code, Data[], TotalCount, OptTime}`; stop when `Data` shorter than `SearchCount`. Per photo: `ID` (32-hex), `Name` (camera file name), `FileName`, `CreateUserID`, `CreateUserName`, `ShootTime` ("YYYY-MM-DD HH:MM:SS"), `Width`, `Height`, `BigImageUrl` (1600px jpg, organizer banner, no EXIF), `DownloadImageUrl` (watermarked full-size, OSS presigned ~12 days). `Code 8` = illegal request.

**xxpie** — API host `https://int.xxpie.com`, every call `platform=H5`. Visitor token: `POST /api/sm/registerVisitorUser {"username":<uuid4 hex>,"platform":"H5"}` → `result.token` (JWT ~15 days), sent as `x-access-token`. `GET /api/pm/queryAlbumStyleH5?album_id=…&is_visited=0&source=H5` → `result.info.album_name`. `GET /api/pm/querySubAlbumPhotoInfo?album_id=…` → `photo_count`, `upload_bys[{sys_user_id, nick_name, total}]`. Listing `GET /api/pm/queryAlbumItemsPgByDefaultSort?album_id=…&page_no=N&page_size=60&sub_album_id=ALL&no_watermark=` → `result.photos[]` (newest-first by upload; short page = end; `count` is always 0). Per photo: `album_ossobject_id`, `file_name`, `record_time` (shot time, **UTC ISO**: `2026-07-11T03:44:26.084Z` = EXIF `11:44:26+08:00`), `width`, `height`, `photographer{nick_name}`, `upload_by`, `url_large1920` (≈2560px long edge, watermark, no EXIF), `url_origin` (watermarked full size with EXIF); image URLs signed, fetch soon after listing. Non-zero `code` → renew token.

**photoplus** — base `https://live.photoplus.cn`, every GET needs `_t` (ms) and `_s = md5("&".join(f"{k}={v}" for sorted params incl. _t, JSON-stringified values with quotes stripped, nulls skipped) + "REMOVED-photoplus-salt")`; send `isNew` as the string `false`. Test vector: params `activityNo=39352660, key='', isNew=false, count=100, page=1, size=2000, ppSign=''`, `_t=7621336002114` → `_s=00000000000000000000000000000000` (`photoplus_sign.py` in the probe folder reproduces it). Wrong `_s` → `{"code":-1,"message":"请求参数不合法"}`. `/live/detail?activityNo=N` → `result.name`. `/album/albums?activityNo=N&count=1000` → sub-albums `{album_id, name, pic_num}`. `/pic/list?activityNo=N&key=&isNew=false&count=100&page=P&size=2000&ppSign=` → `result.pics_total`, `pics_array` (use `ceil(pics_total/100)` pages; `pageTotal` is wrong). `/album/one?albumId=A&count=200&size=200&page=P&ppSign=&picUpIndex=` lists one sub-album. Per photo: `id`, `pic_name`, `relate_time` (shot time, camera-local `YYYY-MM-DD HH:MM:SS` = EXIF DateTimeOriginal), `exif_timestamp`, `width`, `height`, `retoucher`, `retoucher_no`, `big_img` (protocol-relative, 1600px jpg, logo watermark, no EXIF, signed), `watermark_origin_img` (watermarked full size with EXIF). Paid unwatermarked originals ¥18.

**yipai360** — unchanged (see the 2026-09-27/28 specs); previews keep EXIF.

Common: all new-platform previews are ≤2560px, watermarked, without EXIF → shot time comes from the listing. No rate limiting was observed, but pacing limits are unknown.

**Per-photo links (deep-link probe)** — no platform has a URL that opens one photo's detail view; viewers don't change the address bar.
- xxpie: `https://www.xxpie.com/m/albumFilenameSearch?album_id={album_id}&search_word={file_name}` opens a search result showing exactly that photo (verified fresh).
- yipai360: album only, `https://www.yipai360.com/photolivepc/?orderId={orderId}`; the page has a "通过照片名搜索" box that finds the photo by `fname` (verified; not settable from the URL).
- pailixiang: album only, `https://live.pailixiang.com/album/a{code}`; no search box on the page (the API's `SearchText` finds a photo by `Name` or `FileName`, useful for next-phase originals). Photographer page `/grapher/u{code}` exists, untested.
- photoplus: album only, `https://live.photoplus.cn/live/{activityNo}?accessFrom=live#/live`; no search box; `/pic/list` `key` is a paging cursor, not a file-name search. The viewer's info tooltip shows `pic_name`.

## 3. Architecture & decisions

### 3.1 Registry
`data/races.json` (on Ext1TB, gitignored with `data/`), edited only by the CLI (atomic write):
```json
{"races": [{"slug": "2026-gongga100", "name": "2026 贡嘎100",
  "albums": [{"key": "yipai-83415673067642538672", "platform": "yipai", "site_id": "83415673067642538672",  // gitleaks:allow
              "url": "https://www.yipai360.com/…", "title": "FUGA 贡嘎100"}]}]}
```
`photofinder/races.py`: `load() -> Registry`, `race(slug)`, `add_race(slug, name)`, `add_album(slug, url) -> Album` (platform detected from the URL host, `site_id` parsed from the URL, title fetched from the platform's metadata call), `race_dir(slug) = DATA_ROOT/races/<slug>`, `album_dir(slug, key) = race_dir/albums/<key>`. An album belongs to exactly one race; adding the same `key` twice is an error naming the race that owns it.

### 3.2 Layout
```
data/races/<slug>/index.sqlite        one index per race (index.lock beside it)
data/races/<slug>/albums/<platform>-<site_id>/{photos/<source_id>.jpg, manifest.sqlite, download.log, .download.lock}
data/exports/<slug>/<person>/{originals/, photos.csv}
```
`index`, `search`, `eval`, `serve` accept a race slug or a directory path; a slug resolves to the race directory, which until the picker lands (Slice 6) is served exactly like a single collection. A directory without `albums/` is a **single-album collection** (today's behaviour: root `manifest.sqlite` if present) — keeps `data/subsets/*` and the tests working.

### 3.3 Manifest catalog (shared read contract)
Every album's `manifest.sqlite` exposes a `catalog` table or view (§4.2). Scan reads catalogs, never platform tables. yipai keeps its tables and gains a `catalog` **view** (no data rewrite; created by the yipai downloader schema and by the migration). New platforms write a real `catalog` table plus downloader state columns.

### 3.4 Downloaders
- `sources/common.py`: the HTTP policy helpers moved out of `yipai.py` (`looks_like_jpeg`, `write_atomic`, `backoff_seconds`, `retry_after`, `RETRYABLE`, `Blocked`, `RetriesExhausted`, `AlreadyRunning`); `yipai.py` re-imports them so its behaviour and tests stay unchanged.
- `sources/base.py`: `AlbumDownloader` — the paced loop for the new platforms, shaped like yipai's `Downloader.run`: list a page → upsert catalog → download that page's missing previews with a thread pool → on HTTP 403 re-list the page once (signed URLs) → breaker after 20 consecutive image failures → page delay with jitter → final "listed vs reported total" warning. Per-album lock file. Resumable: a photo is done when `status='done'` and its file is a valid JPEG. Defaults: 4 workers, page delay 3 s + jitter, image delay 0.2–0.6 s, 5 tries.
- Adapters `sources/pailixiang.py`, `sources/xxpie.py`, `sources/photoplus.py` each implement: `meta() -> {title, total}`, `pages() -> Iterator[list[CatalogRow]]` (with platform auth/signing), `preview_url(row) -> str` (fresh from the current page), and `site_link(album, row)` (§3.7). yipai is **not** ported onto the base (working, tested; not worth the risk).
- Rows are keyed by `source_id` (pailixiang `ID`, xxpie `album_ossobject_id`, photoplus `id`); new uploads that shift pages are caught by dedupe + rerun (top-up), as with yipai.
- photoplus sub-album (`group_name`): list `/album/albums`, then page each sub-album with `/album/one` so every photo carries its sub-album name (only page 1 of `/album/one` was probed; sub-albums are not known to be disjoint or complete). A photo has exactly one group: the first sub-album it was seen in. Afterwards reconcile against `/pic/list`'s `pics_total`: if fewer distinct ids were seen, page `/pic/list` and add the rest with `group_name` null. The listed-vs-total warning reports any remaining gap.
- Listing times are normalised per adapter to camera-local `YYYY-MM-DD HH:MM:SS`: pailixiang `ShootTime` and photoplus `relate_time` as-is; xxpie `record_time` (UTC ISO) converted to Asia/Shanghai.
- CLI: `photofinder download <race> [album-key]` runs albums sequentially in the foreground; `scripts/download.sh <race> [album-key]` runs it detached under `caffeinate`. yipai albums dispatch to the existing yipai `Downloader` with `out_dir = album_dir`. `scripts/download_yipai.sh` **refuses** once the order id is registered in a race (printing the `scripts/download.sh <race>` command), so a stale top-up can't recreate `data/yipai/<id>/` and re-download 68k photos.

### 3.5 Index
- `db` schema: `photos` gains `album_key text`, `grp text`. `db.connect` migration (idempotent, transactional, like the profiles migration): when `grp` is missing, add both columns and `update photos set grp = album, album = null` — on every legacy index the old yipai tag becomes the Group. This is exact because a legacy `album` value only ever came from a yipai manifest's tag (plain folders have none).
- `scan`: walks the race dir; for each image the album is the `albums/<key>/` prefix of its relpath (or the single-album collection); loads that album's catalog by `stem → CatalogRow`; stores `source_photo_id`, `album_key`, `album` (album title from the registry, `null` for a single-album collection), `grp`, `photographer_uid = "<platform>:<uid>"` (so uids can't collide across platforms), `photographer`; `taken_at` = EXIF DateTimeOriginal if present, else the catalog's `taken_at`.
- This supersedes the 2026-09-27 design's "album = gallery tag" facet: album now means the source album, and the old tag meaning moves to Group (CLI `--album <tag>` becomes `--group <tag>`; no filters are persisted anywhere, so nothing else carries the old meaning).
- Filters: `Filters.groups` added beside `albums`; facets return `albums` and `groups`; CLI `search --group`. The photographer filter matches the name, the full `<platform>:<uid>`, or a bare uid equal to the part after the colon (so existing `--photographer <uid>` usage keeps working).

### 3.6 Server & UI
- `create_app(registry=None, collection=None, …)`: a race-less shell holding the shared `ModelWorker` and a `current: RaceState | None` (`slug, dir, persons, scene_error, originals Job`). `serve` takes an optional slug/path: a slug/none → picker over the registry; a path → that single collection as the only race (dev/E2E on subsets).
- Global endpoints: `GET /api/races` (name, slug, albums with titles and downloaded counts from catalogs, `indexed`, `loaded`), `POST /api/races/{slug}/load`, `GET /api/models`.
- All existing race-scoped endpoints move under `/api/r/{slug}/…` unchanged otherwise. A dependency returns the current `RaceState` or answers **409** `{"detail": "The server switched to <name> — reload", "loaded": <slug>}`.
- Switch boundary: a request binds to the `RaceState` current when it starts and uses only that race's index and data for its whole life, so a request that straddles a switch still reads and writes the race it named (never the newly loaded one). Unloading only drops the server's reference; in-flight requests finish on the old race.
- Load: under a lock; refuses (409) while the current race's originals job runs; drops the current `RaceState` (and upload cache) **before** loading the next, so peak memory is one race plus whatever short in-flight requests on the old race still hold until they finish; runs in a thread; the UI busy bar says "Loading <race name>…". Loading an unindexed race → 400 with the `photofinder index <slug>` hint.
- The browser stores the last race in localStorage (try/catch) and loads it on open; if none, it shows the picker. The top bar shows `Race: [2026 贡嘎100 ▾]` before `Searching for:`.
- Profile ids are per index, so race B's profile 2 is a different person from race A's. The remembered active person is stored **per race slug**; on a race switch the UI uses that race's remembered profile if it still exists, else the first profile. Filters, results, the viewer and My photos are cleared on switch.
- Exports: `profile_folder` uses the race slug (single-album collection: its dir name, as today).

### 3.7 Open on site
`sources.site_link(album, photo) -> {"url": str, "exact": bool, "find_by": str}` — `url` is the most specific page the platform allows (§4.4); `exact` is true only when that page shows just this photo (xxpie); `find_by` is the text that locates the photo once there (the original file name, which every platform shows). Exposed in the photo API (`site`), the viewer and My photos: **Open on site** plus, when not exact, a line with the file name (copy button), shot time, photographer and group, and a platform hint ("search this file name" on yipai; "sort by time / open the group, file name is in the photo info" on pailixiang and photoplus). CSV gets `site_url` and keeps `original_file_name`. For non-yipai photos the originals status reads `open on site`.

### 3.8 One-time 贡嘎 migration
`photofinder race import <slug> "<name>" <collection-dir> --url <gallery url>`:
1. Refuse unless the collection's `index.lock`, `.download.lock` and `data/serve.lock` can all be taken (no indexer, downloader or server running).
2. Back up the index with the SQLite backup API to `data/backups/<collection>-index-<time>.sqlite`; record label/profile counts.
3. `mkdir races/<slug>/albums`; `rename(collection → races/<slug>/albums/yipai-<id>)` (same volume, instant); create the `catalog` view in its manifest; move `index.sqlite` (after `wal_checkpoint(TRUNCATE)`) to the race dir.
4. In one transaction: `relpath = 'albums/yipai-<id>/' || relpath`, `album_key`, `album = <title>`, `photographer_uid = 'yipai:' || photographer_uid` (the `grp` move already happened in `db.connect`).
5. Move `data/exports/<id>/` → `data/exports/<slug>/`; re-point `data/subsets/fullcopy/photos` if it links to the old path.
6. Verify label/profile counts equal step 2's, and every relpath resolves to a file.
7. Register the race and album (last).

Crash safety: recovery is always forward, never a rollback. Every step is idempotent and detects whether it already ran (directory already moved, index already in the race dir, paths already prefixed, exports already moved), so for any interruption point — including after the index moved but before its path rewrite committed — rerunning `race import` completes the import. A verification failure stops before registering and reports; the backup (kept regardless) is the manual restore path. The registry entry is the completion marker: a registered race is never half-migrated, and "already imported" means registered.

### Rejected alternatives
- **One index per album + federated search**: marks and Find more couldn't span albums, and cross-album recall is the point. Rejected.
- **Load all races at start**: user rejected; ~1.3 GB per large race.
- **Race via a header or cookie instead of the URL path**: a stale tab would silently write marks into the wrong race (IDs differ between indexes). Rejected for `/api/r/<slug>/`.
- **Port yipai onto `AlbumDownloader`**: risk to a working downloader for little gain. Rejected this phase.
- **Leave 贡嘎 in `data/yipai/` and symlink it into the race**: `os.walk` doesn't follow directory symlinks, and two paths to one collection invite writes through both. Rejected.

## 4. Data contracts

### 4.1 `races.json` — as §3.1. `slug` matches `^[a-z0-9][a-z0-9-]*$`; album `key` = `<platform>-<site_id>`; platforms: `yipai`, `pailixiang`, `xxpie`, `photoplus`.

### 4.2 `catalog` (per album `manifest.sqlite`)
```
catalog(source_id text primary key, file text,          -- file: 'photos/<source_id>.jpg' when downloaded
        fname text,                                      -- original camera file name
        photographer_uid text, photographer text, group_name text,
        taken_at text,                                   -- 'YYYY-MM-DD HH:MM:SS' camera-local, or null
        width integer, height integer,
        status text not null default 'pending',          -- pending | done | failed
        error text)
meta(key text primary key, value text)                   -- platform-specific ids, e.g. pailixiang AlbumID, xxpie token
```
yipai's view maps `photo_id → source_id`, `fname`, `uid → photographer_uid`, `nickname → photographer`, tag name → `group_name`, `taken_at = null` (EXIF is used), plus `width/height/status/error/file`.

### 4.3 Index `photos` additions: `album_key text` (null for a single-album collection), `grp text`. `album` = album title. `photographer_uid` = `<platform>:<uid>` for race albums.

### 4.4 Site links

| platform | `url` | `exact` | hint |
|---|---|---|---|
| yipai | `https://www.yipai360.com/photolivepc/?orderId={site_id}` | false | paste the file name into 通过照片名搜索 |
| pailixiang | `https://live.pailixiang.com/album/{site_id}` (`site_id` = `a13800138000`) | false | sort by time; file name in the photo info |
| xxpie | `https://www.xxpie.com/m/albumFilenameSearch?album_id={site_id}&search_word={fname}` (fname URL-encoded) | true | — |
| photoplus | `https://live.photoplus.cn/live/{site_id}?accessFrom=live#/live` | false | open the group, sort by time; file name in the photo info |

`find_by` = catalog `fname` for all four.

### 4.5 API
- `GET /api/races` → `[{slug, name, indexed: bool, loaded: bool, albums: [{key, platform, title, url, downloaded: int}]}]`
- `POST /api/races/{slug}/load` → `/api/r/{slug}/facets` body on success; 409 while originals run; 400 if not indexed.
- `/api/r/{slug}/…` = every current `/api/…` endpoint except `/api/models`; 409 `{detail, loaded}` when `slug` isn't loaded.
- `GET /api/r/{slug}/facets` adds `race: {slug, name}`, `groups: [{name, photos}]`; `albums` are source albums.
- `GET /api/r/{slug}/photos/{id}` adds `album`, `group`, `fname`, `site: {url, exact, find_by, hint}`.
- `SearchQuery` adds `groups: list[str]`.

## 5. Slice plan

No slice is parallel-eligible: all touch `cli.py` or build on the previous slice's schema. Run in order.

### Slice 1 — Group filter (album → grp)
Depends on: none.
Tasks:
1. `db`: `album_key`/`grp` columns + idempotent migration (`grp = album, album = null`); `Filters.groups` in `search.filter_where`; facets `groups`; CLI `search --group`.
2. UI: Group dropdown beside Album; photo API/viewer show group.
User-visible: on today's 贡嘎 collection the old yipai tags appear under **Group**; marks unchanged. (Subsystems: index/search, web.)

### Slice 2 — race registry and multi-album scan
Depends on: Slice 1.
Tasks:
1. `races.py` registry (§3.1, §4.1) + `race add` CLI; `index`/`search`/`eval`/`serve` accept a slug or a path; exports folder per race.
2. yipai `catalog` view; multi-album `scan` (catalog per `albums/<key>/`, `album`/`album_key`/`grp`, `<platform>:<uid>` with bare-uid photographer matching, catalog `taken_at` fallback); `originals.rows_of` finds each photo's manifest by `album_key`.
User-visible (verified on a fake two-album race in tests and a symlinked two-album subset): `photofinder index <slug>` indexes all albums; Album filter lists both. (Subsystems: registry/CLI, index.)

### Slice 3 — migrate 贡嘎 into `2026-gongga100`
Depends on: Slice 2.
Tasks:
1. `race import` per §3.8 (locks, backup, rename, relpath/uid rewrite, registry, exports move, fullcopy symlink, verify, rollback); tests on a fake collection with profiles/labels/exports.
2. `download` CLI dispatching yipai albums to the existing `Downloader`; `scripts/download.sh`; `download_yipai.sh` refuses for registered order ids.
3. Real run: on a **copy** of the live index first, then the live one (user's server stopped; restart it on the new code afterwards only if the user wants it running). Update README, CLAUDE.md (layout, rules: `data/races/`), ROADMAP, memory note about the top-up command.
User-visible: `serve 2026-gongga100` shows the same photos, marks and saved people; yipai originals still download into `data/exports/2026-gongga100/`; top-up is `scripts/download.sh 2026-gongga100`.

### Slice 4 — downloader base + pailixiang into 贡嘎
Depends on: Slice 3.
Tasks:
1. `sources/common.py` (moved helpers, re-imported by yipai) and `sources/base.py` `AlbumDownloader` with fake-transport tests (pacing, resume, 403 re-list, breaker, listed-vs-total).
2. `sources/pailixiang.py` (ak, OptTime paging, time normalisation, catalog rows) + `album add` CLI with platform detection and title fetch.
User-visible: `photofinder album add 2026-gongga100 https://live.pailixiang.com/album/a13800138000`, `scripts/download.sh 2026-gongga100`, `photofinder index 2026-gongga100` → 715 more photos searchable; Album filter shows both albums; time filter covers them.

### Slice 5 — Chongli 168 and 四姑娘山 (xxpie, photoplus)
Depends on: Slice 4.
Tasks:
1. `sources/xxpie.py` (visitor token, renewal on non-zero code, `page_no` paging, UTC → Asia/Shanghai).
2. `sources/photoplus.py` (`_s` signing checked against the §Facts test vector, sub-album paging + `/pic/list` reconciliation, protocol-relative URLs).
User-visible: both races registered; Chongli (≈12k) downloaded and indexed; 四姑娘山 download started (operational; its indexing may finish after the slice).

### Slice 6 — race picker
Depends on: Slice 3.
Tasks:
1. Server: race-less shell, `RaceState`, `/api/races`, load/unload, `/api/r/{slug}/` routing, 409 stale-race and originals-running guards, upload cache cleared on switch; `serve` with no argument.
2. UI: race picker, last race and per-race active person in localStorage, `/api/r/<slug>` prefix in the API helper, 409 banner, loading text, state cleared on switch. README/ROADMAP updated.
User-visible: one `photofinder serve`, pick any indexed race; a second tab left on the old race gets the reload banner instead of writing marks.

### Slice 7 — Open on site
Depends on: Slices 5 and 6.
Tasks:
1. `site_link` for all four platforms (§3.7, §4.4); catalog `fname` in the photo API; CSV `site_url`; originals status `open on site` for non-yipai.
2. Viewer and My photos: Open on site, file name with copy button, shot time / photographer / group, platform hint; real-browser check per platform.
User-visible: from any result, one click to the site (xxpie: the photo itself; others: the album plus what's needed to find it).

### Acceptance checklist (whole doc)
1. Migration on a copy then on the live index: profile/label counts and every person's labels unchanged; all relpaths resolve; `index 2026-gongga100` afterwards adds 0 photos; running `race import` twice refuses cleanly.
2. Legacy single-album collections (`data/subsets/race925`) still index, search and serve.
3. Each adapter: fake-transport tests for listing, auth/signing, resume, 403 re-list, breaker; a real paced download of the small albums completes and a rerun downloads 0.
4. Scan gives new-platform photos `taken_at` from the catalog; time filter works on them.
5. Picker: switching A→B→A works; server RSS after the third load within 300 MB of the first load of A; a stale tab gets 409 and the banner; switch refused while originals run; after a switch the active person is that race's remembered one (or its first), never a profile id from the other race.
6. Open on site verified in a real browser for one photo per platform: xxpie shows exactly that photo; yipai's file-name search finds it; pailixiang/photoplus open the right album and the shown file name matches the photo's info there.
7. Real-browser E2E via `photofinder serve` (the user's server stopped first, one server only), screenshots in `data/exports/screens/`.
8. Full `uv run pytest -q` passes.

## 6. Assumed decisions

- A1. **Search scope = race**; saved people, marks and exports are per race (people are not shared across races — kit differs per race anyway).
- A2. **Race slugs** `2026-gongga100`, `2026-chongli168`, `2026-siguniang`; display names as the user gave them.
- A3. **Existing `album` column becomes `grp`** in every index via `db.connect` (yipai tags are sub-albums, not source albums). A pre-change server can't read the new meaning — restart on the new code (same pattern as the profiles migration).
- A4. **Shot time**: EXIF first, then the listing's time normalised to camera-local Beijing time (xxpie's is UTC; the others are local), matching yipai EXIF, so all albums share one time basis. Photographer clock skew stays uncorrected, as in the 2026-09-27 design.
- A5. **Preview variants**: pailixiang `BigImageUrl` (1600), xxpie `url_large1920` (≈2560), photoplus `big_img` (1600). Watermark bands stay; detection copes on yipai's branded previews already.
- A6. **Pacing for new platforms**: 4 workers, 3 s + jitter between pages, breaker at 20 consecutive failures — below yipai's 6 workers because limits are unknown.
- A7. **Open on site** is only exact on xxpie (probe: no platform has a per-photo detail URL). On yipai the file-name search box finds the photo; on pailixiang and photoplus the user browses the album (photoplus 四姑娘山 has 93k photos with no search box, so the group + shot time + file name shown beside the link are what make it findable). Accepted for this phase; next-phase originals remove the need for most photos.
- A8. **Downloads run one album at a time** per `download` invocation; two `download` runs for different races may overlap (different hosts); indexing stays one heavy job at a time.
- A9. **yipai stays on its own downloader**; only helpers move to `sources/common.py`.
- A10. **Storage**: ~743 GB free; new albums ≈ 110k photos × ~0.3–1 MB ≈ 40–60 GB.
- A11. **Photographer uids are prefixed** `<platform>:` in race indexes; the filter also accepts the bare uid.
- A12. **Legacy directories still work** as single-album collections (subsets, tests, `serve <path>`).

## 7. Deferred hardening

- Cross-album duplicates (e.g. 四姑娘山 精选照 is probably a subset of the 93k album): measure overlap by (`fname`, `taken_at`, photographer) after download; if material, collapse duplicates in results. Don't build before measuring.
- Page drift in newest-first listings during a long run (xxpie): rely on dedupe + top-up rerun; add an ascending sort only if a rerun keeps finding missed photos.
- Token/OptTime expiry mid-run beyond one renewal.
- Catalog metadata revisions (group, photographer name, time) after a photo was indexed are not propagated by the incremental scan; add a metadata refresh pass if top-ups show changes.
- Photos in several photoplus sub-albums keep only their first group; support multiple memberships if overlap turns out common.
- Race-load memory: loading transiently needs ~3× the float16 matrix (~2.7 GB for 贡嘎, more for 四姑娘山); measure while an indexer runs on another race and add a pressure check before load only if it swaps.
- Registry edits while a server is running (the picker reads `races.json` on each `/api/races`, so new races appear; no live reload of a loaded race's albums until it's reloaded).
- Originals for new platforms (next phase): free watermarked full-size files — per-photo lookup via pailixiang `SearchText` and xxpie file-name search; photoplus has no lookup, so re-list (by sub-album) to get fresh signed URLs.

## 8. Review status

spec-review: completed 2026-09-29
