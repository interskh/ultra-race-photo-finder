# Indexer memory cap — handoff (2026-09-29)

Goal from the user: keep indexing under 2 GB (1 GB if possible). Outcome: 4 GB default (`--max-memory`, user's choice after the measurements below); 2 GB is not reachable for person embedding on this machine at any usable speed.

## Measured (macOS process footprint, sampled every 0.1 s from outside the spawned stage process)
Real `photofinder index` runs, fixes below in place, limit raised to 8192 so nothing restarted:

| stage | where | peak | speed |
|---|---|---|---|
| detect (batch 1) | 500-photo Chongli sample (1600px and 2560×3413) | 1.9 GB on 1600px, 2.24 GB once 2560px photos start | 11 photos/s |
| embed_persons (batch 64, OSNet sub-batch 32) | Chongli, 7,150 people | 3.77 GB | 28 people/s |
| embed_persons | Chongli sample, 1,598 people | 3.88 GB | 25 people/s |
| embed_scenes (batch 4) | Chongli, 11,939 photos | 2.00 GB | 23 photos/s |

Before: embed_persons stopped at 6.3 GB under the old 6 GB guard; detect at 6.1–6.8 GB (batch 8).

Where embed_persons' ~3.8 GB goes (probe on 64 Chongli crops):
- torch import ~0.15 GB.
- Metal/MPS working memory ~1.1 GB, the same at SigLIP batch 8, 16, 32 and 64, fp16 or fp32; `torch.mps.empty_cache()` does not release it. Shared between OSNet and SigLIP.
- SigLIP2 image tower after load ~1.0 GB (fp32; weights are 0.37 GB, the rest is load residue); fp16 0.7 GB. Loading the whole model (text tower too) was ~1.75 GB.
- OSNet ~0.13 GB; its working memory is 2.2 GB at batch 64 and 1.1 GB at 32 or less (speed: 144 / 123 / 88 crops/s at 64 / 32 / 16; SigLIP is the bottleneck at ~56/s).
- Decoded photos: 2560×3840 photos are ~30 MB each; macOS keeps the freed pages in the footprint (22 held → 927 MB, released → 820 MB).

## Decisions
- Each model stage runs in a spawned child (`cli.run_model_stage`), restarted fresh when it reaches the cap (content-dependent leak of ~0.5–1.75 MB/photo during detect). A cap that the first batch alone exceeds stops with "raise --max-memory".
- Indexing loads only SigLIP2's image tower (`models.text_tower = False` in the child); embeddings identical to the full model (max |diff| 0).
- embed_persons and ocr_bibs decode one photo at a time (rows are ordered by photo), cut its crops and drop it; before, the whole batch's photos were held (up to 64 × 30 MB).
- OSNet runs in sub-batches of 32 inside `embed_crops`; outputs bit-identical to batch 64.
- SCENE_BATCH 16 → 4: holds 4 decoded photos instead of 16; 23 photos/s measured.
- Detect batch 1 (measured faster than 8 and ~0.5 GB of driver memory per extra image).
- Default 4096 MB: clears every stage's measured peak (max 3.9 GB) at full speed. The cap is checked between batches, where embed_persons sits at 3.28–3.34 GB (the 3.8 GB peaks are inside a batch), so ~750 MB of headroom before a restart.

## Whole-run gate (fresh reviewer + Codex), fixed before commit
- Stage child outlived a killed parent (lock released while the child kept writing): the child now runs `memory.watch_parent` (moved from `web/app.py`, same as the server's model worker).
- First-batch stop counted items, so two small batches under memory pressure (32 + 16) stopped the run: now counts batches.
- A restart that saved nothing (e.g. OCR failures left pending) could loop forever: stops when the pending count equals the previous run's.
- `models.text_tower` stayed False after an in-process run (tests; broke the opt-in real-model text test): restored in `stages.run`.
- `stages.run` was never exercised by the restart tests: added a real embed_scenes run that restarts once and resumes from saved rows.
- README said "capped": now says checked between batches.
- Not exercised end to end: killing the parent of a running stage child (same mechanism as the server worker).

## Rejected (measured)
- CPU for either embedding model: OSNet 28 vs 123 crops/s, SigLIP 26 vs 56/s, and SigLIP on CPU peaks ~2.1–2.8 GB anyway.
- Building SigLIP on the meta device and loading image weights straight to MPS: 1.6 GB after load, worse than the CPU-then-move load (1.0 GB).
- fp16 SigLIP for indexing: saves ~0.3 GB only, and indexing stays fp32 (index embeddings match the eval baselines).
- Smaller SigLIP batches: no memory saved (working memory is flat), slower.
- Preprocessing scenes to 224×224 tensors at load: exact, but ties the stage to the model's transform; batch 4 got most of the saving.

## Deferred
- Splitting embed_persons into an OSNet pass and a SigLIP pass (each ~2.5–3 GB, extra decoding) — not measured.

## Tests
- `tests/test_index_isolation.py`: restart until done, first-batch stop, small batches under pressure still restart, no-progress restart stops, killed child resume hint, `--max-memory` passed to every stage, default 4096, batcher counts, a real stage restarting and resuming from saved rows, real spawned child. Mutations (item-count first-batch check, no saved-nothing check, text_tower not restored) each fail a test.
- `tests/test_detect_embed.py`: embed_persons holds at most one decoded photo and none while embedding; OSNet sub-batches of 32 in order. `tests/test_ocr_bibs.py`: ocr_bibs holds one decoded photo while reading. Mutation (keep the batch's photos alive) fails both one-photo tests.
