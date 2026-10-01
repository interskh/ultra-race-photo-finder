# ultra-race-photo-finder

[中文说明](README.zh-CN.md)

Find your own photos among tens of thousands of race photos, even when your face is covered or the photo was taken at night. Search by clothing, bib number, time, photographer or scene ("雪山", "finish arch"), mark the hits as you, and let it find more. Built for the photo-live galleries that Chinese trail and ultra races use: 一拍即传 (yipai360), 拍立享 (pailixiang), 享像派 (xxpie) and PhotoPlus (谱时). Everything runs locally on a Mac. No accounts, no cloud.

The CLI and Python package are called `photofinder`.

## How it works

1. **Download** a race's public gallery previews, paced so the sites aren't hammered.
2. **Index** them locally: person detection (YOLO), clothing re-ID (OSNet) and image/text embeddings (SigLIP2), plus optional bib OCR (Apple Vision).
3. **Search** in the browser. Start from your bib, a photo of yourself, or a description. Mark results ✓ Me / ✗ Not me, then **Find more like my marked ones**, which brings up other photographers' shots of you. Shots taken just before and after a confirmed photo are listed next to it.
4. **Export** your photos: a CSV, the full-size originals the site serves for free, or a zip.

Clothing similarity alone is weak when hundreds of runners wear the same event jacket. What works is the loop of bib → mark → Find more → mark. Measured numbers are in [the design notes](docs/handoff/2026-09-27-photo-finder-search-design.md).

## Supported galleries

| Site | Album URL | Previews downloaded | Originals (for photos you mark) |
|---|---|---|---|
| 一拍即传 yipai360 | `www.yipai360.com/…?orderId=…` | 1920 px, watermarked | Full size with EXIF, as the site's 下载 button gives it (some galleries add the organizer's branding band) |
| 拍立享 pailixiang | `live.pailixiang.com/album/<id>` | 1600 px | Free full-size watermarked copy, no EXIF |
| 享像派 xxpie | `www.xxpie.com/m/album?id=<id>` | ~2560 px, watermarked | Free full-size watermarked copy with EXIF |
| PhotoPlus 谱时 | `live.photoplus.cn/live/<id>` | 1600 px, watermarked | Free full-size copy with the photographer's logo watermark |

Unwatermarked originals are paid on every site. **Open on site** links each photo to its gallery so you can buy it there.

## Requirements

- A Mac with Apple Silicon (models run on MPS; bib OCR uses Apple Vision). Developed on 16 GB of memory; the indexer restarts a model stage once its process passes 4 GB (checked between batches, so it can briefly go over).
- [uv](https://docs.astral.sh/uv/) (Python 3.13 is installed by uv).
- Disk space: about 1.5 GB of model weights (downloaded on first use), plus roughly 0.6 GB per 1,000 photos for previews and the index. A 12,000-photo race uses about 7 GB.

## Install

```
git clone <this repo> ultra-race-photo-finder
cd ultra-race-photo-finder
uv sync
```

All data goes into `data/` inside the checkout (gitignored). To keep it elsewhere, such as an external disk, create the folder and set `PHOTOFINDER_DATA_ROOT=/path/to/data`; a missing custom folder is refused, so an unmounted disk fails loudly instead of filling the wrong drive.

## Quick start

```
uv run photofinder race add 2026-myrace "2026 My Race"                    # a race groups one or more albums
uv run photofinder album add 2026-myrace https://live.pailixiang.com/album/a13800138000
scripts/download.sh 2026-myrace                                           # detached, resumable; rerun later to top up
tail -f data/races/2026-myrace/download-console.log
uv run photofinder index 2026-myrace                                      # add --ocr to read bib numbers (slow)
uv run photofinder serve                                                  # http://127.0.0.1:8000/
```

Indexing time depends on the race. On an M4 Mac, about 96,000 photos took 4 h 37 min. Indexing is incremental, so after a top-up download rerun `index` and only the new photos are processed.

In the browser, pick the race, then type your bib number or upload a photo of yourself and click your box. Full guide: [docs/usage.md](docs/usage.md).

## Data layout

```
data/races.json                                         race registry (written by the CLI)
data/races/<race>/index.sqlite                          one index per race
data/races/<race>/albums/<platform>-<id>/photos/        downloaded previews
data/races/<race>/albums/<platform>-<id>/manifest.sqlite  catalog: photographer, shot time, group
data/exports/<race>/<person>/{originals/, photos.csv}   your exports
data/models/                                            model weights and caches
```

| Variable | Default | Purpose |
|---|---|---|
| `PHOTOFINDER_DATA_ROOT` | `<checkout>/data` | Registry, races, exports, server lock |
| `PHOTOFINDER_MODELS_DIR` | `<checkout>/data/models` | Model weights; not affected by `PHOTOFINDER_DATA_ROOT` |

## Please be polite to the sites

The downloaders fetch only what a visitor's browser can see, and they are paced on purpose: a few workers, delays between pages, a circuit breaker, and one original lookup every few seconds. Please keep it that way. Use the tool for your own photos (or friends' photos, with their consent), respect the photographers' copyright and each site's terms, and buy the unwatermarked photos you want to keep. The sites' private APIs can change at any time, which will break the matching downloader.

## Docs

- [docs/usage.md](docs/usage.md): the web UI and CLI in detail.
- [docs/superpowers/specs/](docs/superpowers/specs/): design documents (architecture, data model, scoring, races and albums).
- [docs/handoff/](docs/handoff/): build logs with decisions, rejected alternatives and measured results.
- [docs/ROADMAP.md](docs/ROADMAP.md): status, next steps and known issues.

## Development

```
uv run pytest -q
```

Tests use fake HTTP servers and stub models, so they need neither network access nor model weights. `PHOTOFINDER_REAL_MODELS=1` enables one test that loads the real models. `CLAUDE.md` contains the working rules for coding agents.

## License

[AGPL-3.0-or-later](LICENSE). The person detector (Ultralytics YOLO) and re-ID library (BoxMOT) are AGPL-3.0.
