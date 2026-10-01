# photofinder

Find your own photos among thousands of race photos — by clothing, bib number, time, photographer, or scene ("雪山", "finish arch"). Local only; runs on an Apple Silicon Mac. All photos, indexes and model weights live on `/Volumes/Ext1TB`.

Design: `docs/superpowers/specs/2026-09-27-photo-finder-search-design.md` · build log and measured results: `docs/handoff/2026-09-27-photo-finder-search-design.md` · races and albums: `docs/superpowers/specs/2026-09-29-multi-race-albums-design.md`.

## Races

A race groups one or more source albums and has one index, so one search spans all its albums. Races are registered in `data/races.json`:

```
uv run photofinder race add <slug> "<name>"      # e.g. 2026-gongga100 "2026 贡嘎100"; registers a race with no albums yet
```

Add an album by its gallery URL (yipai360, pailixiang, xxpie or photoplus; the platform is read from the URL):

```
uv run photofinder album add <slug> <album URL> [--title "<title>"]   # e.g. album add 2026-gongga100 https://live.pailixiang.com/album/a13800138000
```

The race, the URL and "album already in a race" are checked before anything is sent to the site. Without `--title`, the title is fetched from the site where the downloader supports it (pailixiang, xxpie, photoplus); yipai albums are registered with no title and the Album filter shows the key (e.g. `yipai-<orderId>`). If the title fetch fails, rerun with `--title`. Then `scripts/download.sh <slug>` and `photofinder index <slug>`.

**One-time move of an old yipai collection** (`data/yipai/<orderId>/`) into a new race, without re-indexing and keeping marks and saved people:

```
uv run photofinder race import <slug> "<name>" data/yipai/<orderId> --url "<gallery URL>" [--title "<album title>"]
```

It creates the race itself (don't `race add` it first). The URL's orderId must match the directory name and the manifest. It refuses while an indexer, a download of that collection, or the server holds its lock. It backs the index up to `data/backups/`, moves the collection to `data/races/<slug>/albums/yipai-<orderId>/` and the index to `data/races/<slug>/`, moves `data/exports/<orderId>/` to `data/exports/<slug>/` (rewriting the paths in each `photos.csv`), re-points `data/subsets/*` symlinks, checks that marks and saved people are unchanged and every photo resolves, and registers the race last. If it is interrupted, rerun the identical command; it finishes the remaining steps. Start nothing on that collection (download, index, serve) until it prints its summary; while an import is unfinished, `download_yipai.sh` refuses that order id. Afterwards `photofinder index <slug>` should scan 0 new photos. `--title` (default: the race name) is what the Album filter shows.

Layout:

```
data/races.json
data/races/<slug>/index.sqlite
data/races/<slug>/albums/<platform>-<id>/{photos/, manifest.sqlite, download.log}
data/exports/<slug>/<person>/{originals/, photos.csv}
data/backups/                                    # index backups taken by race import
```

`index`, `search`, `eval` and `serve` take a race slug or a collection directory (any folder of JPEGs, e.g. `data/subsets/race925`).

## 1. Download

```
scripts/download.sh <race> [album-key]           # e.g. scripts/download.sh 2026-gongga100
tail -f data/races/<race>/download-console.log   # per album: data/races/<race>/albums/<key>/download.log
```

Runs `photofinder download <race> [album-key]` detached under `caffeinate`: the race's albums one after another (or only the given one), paced, resumable. Rerun the same command a day or two later to pick up photos uploaded after the race. yipai360, pailixiang, xxpie and photoplus albums download (pailixiang: 1600px previews; xxpie: watermarked previews of about 2560px; photoplus: 1600px watermarked previews, each photo tagged with the first sub-album it appears in as its Group, then the rest of the album with no group; all 4 workers). The yipai downloader fetches only the free 1920px previews; full-size originals of the photos you mark come later from the web UI (step 3).

A yipai gallery not in any race still downloads the old way, `scripts/download_yipai.sh <orderId>` into `data/yipai/<orderId>/`. It refuses an order id registered in a race and names the `scripts/download.sh <race>` command to use instead.

## 2. Index

```
uv run photofinder index <race>                  # or a collection directory
```

Stages: scan (EXIF time, photographer, album, group) → detect people → clothing embeddings → scene embeddings. Add `--ocr` to also read bib numbers with Apple Vision (optional and slow: ~2–3 h for 190k people). Resumable and incremental; throttles under memory pressure. Each model stage runs in its own process; when that process's footprint is over 4 GB (`--max-memory MB`, checked between batches, so a batch can briefly go past it) the stage restarts in a fresh process and carries on. Measured peaks on 2560px photos: detect ~2.2 GB, clothing embeddings ~3.9 GB, scene embeddings ~2.0 GB. A cap below a stage's first batch stops with a message naming the stage.

## 3. Search in the browser

```
uv run photofinder serve                         # race picker, http://127.0.0.1:8000/
uv run photofinder serve <race>                  # same, with that race preloaded
uv run photofinder serve <collection-dir>        # serve one directory alone (e.g. data/subsets/race925)
```

With no argument the page opens on the race picker: pick any indexed race from **Race:** at the top (races that are not indexed yet are greyed out and name the `photofinder index <race>` command). The server keeps one race loaded at a time; switching unloads the previous race (loading a large race takes up to a minute) and clears the page's search, filters and viewer. A page opens on the race the server already has loaded (e.g. `serve <race>`); only when nothing is loaded does it load the last race this browser used, and otherwise shows the picker. Per race, the browser also remembers the last person you searched for. A second tab still on the old race shows a banner with a Reload button instead of writing marks into the wrong race. Switching is refused while an originals download runs.

Only one server runs at a time: a second `serve` exits right away and names the running one (pid, URL, collection).

The server runs the models (person detector, re-ID, SigLIP2 in half precision) in a separate worker process that starts on the first upload or description/scene search and exits after 5 minutes without one, so the OS reclaims all of its memory; the server itself stays at its baseline (~1.3 GB, mostly the embedding index). Starting the worker takes about 20 s (the busy bar says "Loading the … models") and peaks at ~3.5 GB while the checkpoint loads. If the worker dies (e.g. killed under memory pressure), that request fails with "try again" and the next one starts a fresh worker.

What works best (measured in the handoff log): clothing alone is weak when many runners wear the same event jacket, so iterate:

1. Upload a photo of you (race day, same kit) and click your box — or start from your bib number if the index was built with `--ocr`.
2. Mark results **✓ <name>** / **✗ Not <name>** (the active person, "Me" by default).
3. **Find more like my marked ones** — searches with all your marked shots, which is how other photographers' photos of you surface. When the search uses two or more marked people, each result shows a small **matched via** thumbnail: the marked photo it resembled most; click it to jump to that photo in My photos. Changed clothes (jacket on/off)? Mark one photo of each look.
4. Narrow with time, photographer, album, group; add a scene or outfit description.
5. **My photos** lists the marked photos with each one's original status (✓ original / `buy on site: <reason>` / `failed: …` / `open on site`):
   - **Download originals** (yipai360 galleries only) fetches the full-size originals into `data/exports/<race>/<person>/originals/<YYYYMMDD-HHMMSS>_<photographer>_<source photo id>.jpg` — one lookup every 6 s (the site rate-limits file-name searches), skips files already there, shows `n / N`, the current file and errors, and can be cancelled; rerun to resume. Photos the site refuses are listed as `buy on site: <reason>`. In a race that mixes platforms, photos from pailixiang, xxpie and photoplus albums are skipped without any request and show `open on site` (originals come only from yipai360).
   - **Download as zip** streams that person's originals folder plus `photos.csv` to the browser (e.g. to move them to a phone).
   - **Export CSV** writes `data/exports/<race>/<person>/photos.csv` (UTF-8 with BOM, opens in Excel): source photo id, original file name (searchable on the site), photographer, time, album, group, preview/original paths, download status and `site_url` (the photo's page on its site: on xxpie the photo itself, elsewhere its album — find the photo there by the original file name).
   - In the photo viewer, **Download original** fetches one photo, saves it into the same folder and hands it to the browser (yipai360 photos only; hidden for other platforms).
   - **Open on site** (on each My photos card and in the viewer) opens the photo's site in a new tab so you can buy or download it there. On xxpie it lands on the photo itself. Elsewhere it opens the album, and the viewer shows the original file name with a **Copy** button plus a hint: on yipai360, paste the file name (with extension) into the album's 通过照片名搜索 box and press Enter; on pailixiang look near the shot time and check 照片信息 under a photo; on photoplus open the group's tab and check the ⓘ icon under a photo. Where the browser blocks clipboard access (plain http on a LAN address), Copy selects the name so you can press ⌘C.

   Originals are exactly what the site's own 下载 button gives: full resolution with EXIF, but for FUGA galleries with the organizer's branding band along the bottom (the signed URL applies it). An unbranded source was not probed.

**Nearby shots.** Photographers shoot bursts, so the shot just before or after a confirmed photo of you often shows you too (same photographer ±1 shot ~60%, ±2 ~40%, ±3 ~25%, measured on bib reads). Find more lists **Next to your marked photos** above the ranked results, bib searches list it below the exact bib hits: the unmarked shots within ±N that were taken at most 30 s from the confirmed photo (same runner by gap: ≤2 s 73%, ≤5 s 26%, ≤10 s 10%, >30 s ~2%; default N 2, **Include nearby shots** toggle and ±1–5 stepper, remembered in the browser), each badged with its gap (`2 s after`, `same sec`); click the badge to see the photo it sits next to. In the viewer, a photo you marked (or a bib hit opened from a bib search) shows a **Same photographer · before / after** strip of ±3 shots; click a shot or press `,` / `.` to step through the roll, the likely same runner is pre-selected, so `M` marks it. Marking a shot re-centres the strip on it.

**Several people.** `Searching for: [Me ▾]` in the top bar switches between saved people; **+ New person** adds one (e.g. a friend), **Rename** / **Delete** act on the active one (delete removes only that person's marks; the last person can't be deleted). Each person has their own marks, Find more, My photos, CSV and originals folder. The browser remembers the active person.

**Keyboard.** On a focused result card (Tab to it) or in the photo viewer: `M` = this is <name>, `N` = not <name> (press again to clear), `←` / `→` previous / next result, `,` / `.` previous / next shot in the photographer's roll (viewer strip), `Esc` closes the viewer. Shortcuts are off while typing in a text box.

**Upgrading an existing index.** The first time this version opens an index made by an older version it moves the old Me / Not me marks into the person "Me" (one-way). A server still running the older code on that index can no longer save marks — restart it on the new code.

## CLI search / evaluation

```
uv run photofinder search <race> --photo me.jpg [--box N] [--scene 雪山] [--bib 8038] [--from ... --to ...]
uv run photofinder eval <race> --bib 8038            # recall of clothing search against OCR'd bib ground truth
```

## Rehearsals and dev: another data root

`PHOTOFINDER_DATA_ROOT=<dir>` points the CLI and both download scripts at another data folder (registry, races, exports, backups, subsets, `serve.lock`); model weights still come from the real `data/models`. Set it when running from a git worktree: the scripts otherwise use the checkout's own `data/`, while the CLI uses `/Volumes/Ext1TB/Projects/photo-finder/data`. Caveat: `serve.lock` follows it, so a server started under another root doesn't see the real one; stop the real server first, since only one may run machine-wide.

## Tests

```
uv run pytest -q
```
