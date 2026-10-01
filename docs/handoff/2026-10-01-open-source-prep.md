# Open-source prep (2026-10-01)

Goal: publish the repo as `ultra-race-photo-finder`: folder structure review, an English and a Chinese README, and no private data in the tree or the history.

## Decisions
- **Name**: distribution `ultra-race-photo-finder`; Python package and CLI stay `photofinder`, because renaming them churns every file and command for no user benefit. `[tool.uv.build-backend] module-name = "photofinder"` is required once the project name differs from the module (uv build-backend docs). The local folder is not renamed (owner, 2026-10-01).
- **License**: AGPL-3.0-or-later (canonical GNU text). ultralytics and boxmot are AGPL-3.0 (checked in installed metadata), so the project must be AGPL-compatible.
- **Data root**: was a fixed path on the dev machine's external disk. Now `config.DEFAULT_DATA_ROOT` is `<checkout>/data` when `src/photofinder/config.py` sits in a checkout (pyproject.toml two levels up), otherwise `./data`. `require_mounted` creates the default root, but still exits for a missing `PHOTOFINDER_DATA_ROOT` (unmounted disk). The new `PHOTOFINDER_MODELS_DIR` keeps worktree runs from re-downloading the weights. The CLI and scripts now agree: both used to resolve `data/` differently from a worktree.
- **Docs**: kept public after redaction (owner, 2026-10-01): CLAUDE.md, handoffs, specs, ROADMAP. README split into a short overview (EN + zh-CN, cross-linked) and `docs/usage.md` (the former README body, verbatim, plus a data-root section). Personal operational sections were removed from the ROADMAP (live collection paths, top-up commands, label-migration note); the scale measurements were kept, without race names or paths. Chinese brand names come from the sites' own titles and meta tags: 一拍即传, 拍立享, 享像派, PhotoPlus (谱时).
- **History**: rewritten and kept (owner, 2026-10-01: author name, gmail and Claude trailers stay). `git filter-repo` with a replace-text list kept outside the repo replaces the following in every commit and message:
  - photographer and studio names with neutral placeholders
  - the file name of one of the user's own originals
  - the pailixiang album slug (Chinese mobile-number shape) with `a13800138000`
  - third-party account ids in the fixture JSON with synthetic ids of the same shape

  `docs/handoff/2026-09-29-platform-probes/`, the raw site API dumps with third-party ids and scratch paths, is removed from all history. The private copy is kept under `data/` and is gitignored.
- **Folder structure**: kept as is: `src/` layout, `tests/fixtures`, `scripts/`, `docs/{handoff,superpowers/specs}`. `docs/superpowers/specs` stays because the spec tooling writes there; the README's Docs section explains each folder.

## Rejected
- A fresh single-commit history. the owner chose to rewrite and keep the history.
- Renaming the package or CLI to match the repo name.
- Moving the dev docs out of the repo.

## Deferred
- Optional cleanup of personal migration paths that new users never need: `race import`, `scripts/download_yipai.sh` and the legacy `data/yipai/` layout.
- `pyproject` `requires-python >=3.13` plus macOS-only dependencies (ocrmac/pyobjc): Linux installs fail at `uv sync`. Documented as Mac-only.
