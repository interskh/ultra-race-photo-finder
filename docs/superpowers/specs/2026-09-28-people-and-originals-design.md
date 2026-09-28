# Saved people, originals download, "matched via" — design

Date: 2026-09-28 · Status: approved in chat ("sounds good, add #2 and a bunch of other improvement (for download etc)").

## Why

The user has marked their own photos ("me") in the web UI and now needs to (a) get the full-size originals, (b) run separate searches for themselves and for friends without mixing marks, and (c) handle layer changes (jacket on/off) — which already works via max-over-references scoring if each look has one marked example, so the UI should show *which* marked photo each result matched.

## Facts established (2026-09-28)

- For this gallery the site's 下载 button downloads the full-size original for free (4200×2800, ~3.6 MB, EXIF intact). The API photo's `img.primary + img.path + img.sign` URL returns byte-identical content (verified on photo 11970653 / `_7647374.JPG`).
- Signed URLs expire (~1 day), so each original needs a fresh URL. The list endpoint accepts a file-name filter: `GET /api/v1/yipai/order/<orderId>/audience/photos?tagId=&pwd=&sortType=desc&page=1&pageSize=100&fileName=<fname stem>` (same headers as the downloader). File names are NOT unique (e.g. `未标题-1.jpg`), so match results by `photoId`; if not found in the first page, page through that filtered result set.
- In paid galleries the same URL may be watermarked or refused; treat a non-JPEG / 403 as "buy on site" and keep going.
- The live index (`data/yipai/83415673067642538672/index.sqlite`, 792 MB) holds the user's real labels (10 × `me`). They must survive the migration.

## Design

### 1. Saved people ("profiles")
- Schema: `profiles(id pk, name unique not null, created_at)`; labels become `labels(profile_id, person_id, label, created_at, primary key(profile_id, person_id))`.
- Migration in `db.connect` (idempotent, in a transaction): if the old single-identity `labels` table exists, create profile **"Me"** and move every existing label into it. Never drop data on failure.
- API: list/create/rename/delete profiles (delete asks for confirmation in UI; deleting removes only that profile's labels); every labels / find-more / my-photos / export / originals call takes a `profile_id`.
- UI: top bar `Searching for: [Me ▾]` with "+ New person" and rename; the active profile is remembered in localStorage (fallback: first profile). Label buttons use the profile name: **"✓ <name>" / "✗ Not <name>"** (tooltip spells it out).

### 2. "Matched via" (layers)
- For results of Find more (and any multi-reference search), return which reference person produced the max similarity and show its small thumbnail on the result card ("matched via ▢"). Clicking it highlights that reference in My photos.
- My photos groups nothing automatically; it simply lists marked photos. A one-line hint in Find more explains: "Changed clothes? Mark one photo of each look."

### 3. Originals download + list
- Per profile, "My photos" gets **Download originals**: a background job in the server (one at a time per collection) that, for each marked photo (yipai collections only): looks up a fresh signed URL via the file-name filter, downloads the original with the downloader's pacing/UA/backoff (reuse `photofinder.sources.yipai` helpers — no new HTTP policy), validates JPEG, writes atomically.
- Output: `data/exports/<collection>/<profile-name>/originals/<YYYYMMDD-HHMMSS>_<photographer>_<source_photo_id>.jpg` (chronological, filesystem-safe names), skipping files already present; plus `photos.csv` (UTF-8 with BOM for Excel): source_photo_id, original file name (searchable on the site), photographer, taken_at, album, preview path, original path, status (`downloaded` / `buy on site: <reason>`).
- Progress: job status endpoint polled by the UI (n / N, current file, errors); cancel button; rerun resumes.
- **Download as zip** button streams a zip of that profile's originals folder to the browser (for moving to a phone).
- Viewer: **Download original** for the single open photo (same fetch path, saves into the active profile's folder and also offers it to the browser).
- Non-yipai collections: originals buttons hidden; CSV still exported with local paths.

### 4. Small UX improvements
- Keyboard in the viewer and grid focus: `M` = this is <name>, `N` = not <name>, `←/→` prev/next, `Esc` close.
- My photos shows count per profile and the download status of each photo (downloaded ✓ / buy on site).

## Acceptance checklist
1. Migration on a copy of the live index keeps all 10 `me` labels under profile "Me"; running it twice is a no-op; old-schema tests pass.
2. Labels, Find more, My photos and export are isolated per profile (unit + API tests).
3. Find more results carry the matched reference id; the UI shows its thumbnail.
4. Originals: fake-API tests for fresh-URL lookup by file name (duplicates resolved by photoId, paging), 403/non-JPEG → "buy on site", resume skips existing, cancel stops; real run downloads the 10 marked originals from a copy of the live index into `data/exports/...`, each 4200-px-class JPEG with EXIF.
5. Zip download and single-photo download work in a real browser; screenshots of profile switcher, matched-via, originals progress, CSV.
6. Full `pytest` passes.

## Slice plan

### Slice 1 — people, matched-via, originals
Depends on: none.
Tasks:
1. Profiles schema + migration + per-profile labels/find-more/my-photos/export in `db.py`, `search.py`, `web/app.py`; "matched via" reference id in search results.
2. Originals job (fresh URL by file name, paced fetch via yipai helpers, naming, CSV, zip, single-photo) + API endpoints.
3. UI: profile switcher, matched-via thumbnail, originals progress/zip/CSV, viewer download, keyboard shortcuts; real-browser E2E on a copy of the live index.
