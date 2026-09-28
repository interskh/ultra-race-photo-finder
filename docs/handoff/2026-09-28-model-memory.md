# Server model memory — handoff

Date: 2026-09-28/29 · Branch `implement-loop/2026-09-28-model-memory` · Request: "yes we do both (and have an indicator that model is loading)", then "we should really limit our memory usage, the box getting killed".

## Measured before

The `serve` process sat at 2851 MB: SigLIP2 fp32 on MPS (IOAccelerator 1465 MB, loaded on the first upload/text query, never released) plus ~1.1 GB of embeddings and working memory. The idle baseline before any model loads is ~1.33 GB.

## Decisions

- **Models run in a spawned child process** (`web.app.ModelWorker`, a `ProcessPoolExecutor(1)` with the `spawn` context). The server stops it after 300 s without model work. Every model call (`encode_text`, `detect_and_embed`) goes through it, so the parent never imports torch.
  - Why: unloading inside a long-lived process doesn't release memory. Three load/unload cycles (encode_text → `models.unload()` → 5 s pause) left **545 → 1453 → 1805 MB** after each unload (fp16) and 541 → 1655 → 1933 MB (fp32). The reload footprint grew 1977 → 2885 → 3748 MB. The reviewer saw 3.7 GB on the live server after one reload. A child process that exits returns everything to the OS.
  - The idle timer counts work from the executor future's completion, not from the `await`. A cancelled request (client gone) therefore doesn't start the idle clock while the child is still working (Codex finding).
  - A child killed mid-request fails that request with 503 "try again"; the pool is dropped and the next call spawns a new child. A child killed while idle is detected on the next `submit` (BrokenProcessPool) and replaced.
  - Don't call `pool.shutdown()` from a future's done-callback on a broken pool. CPython runs those callbacks while holding the pool's shutdown lock, so it deadlocks (reproduced; covered by `test_worker_killed_mid_request_…`).
  - The child runs `watch_parent` (polls `os.getppid()` every 2 s), so a SIGKILLed server can't leave a 2 GB orphan.
  - YOLO is no longer unloaded after each upload's detection; the whole child exits when idle.
- **SigLIP2 fp16 (`pure_fp16`) in the server only.** `cli serve` sets `models.half_precision`, which is passed to the child as an initializer argument. Indexing stays fp32 because the index embeddings are fp32.
  - Fidelity on race925 (48 random person crops + 8 texts): fp32-vs-fp16 cosine ≥ 0.99997 (image) and 1.00000 (text); top-10 / top-50 overlap against the index is 99.6% on average (min 90% / 96%). No NaNs.
  - Gain is smaller than the 0.75 GB estimate: weights 1431 → 716 MB, but the MPS allocator holds 1225 MB of heap vs 1465 MB for fp32, so the process saves ~240 MB (2213 → 1977 MB). Moving parameters to MPS one by one from a CPU fp16 copy got to 1814 MB. That wasn't adopted because it bypasses open_clip's loader.
- **Loading indicator.** `GET /api/models` returns `{running, ready, loading, unload_after_s}` from parent-side state: `loading` is the first in-flight call (by function name) that hasn't completed in the current child. The UI polls it while busy and shows "Loading the text search / person detection models (~20 s)…", with a tooltip about the 5-minute unload.

## Whole-run gate (reviewer + Codex), fixed in its own commit

- **Deadlock (both reviewers, confirmed in CPython 3.13 `concurrent/futures/process.py:505`):** `terminate_broken` holds the pool's `_shutdown_lock` while it fails pending futures, which runs `finished()` (needs `ModelWorker.lock`), while `submit()` held `ModelWorker.lock` and called `pool.submit()` (needs `_shutdown_lock`). Fix: never call into the pool while holding `ModelWorker.lock`. `submit` reserves a `running` slot under the lock, then submits outside it; a pool that's already broken is replaced and the call retried. `on_models` calls `submit` via `run_in_threadpool`, so the event loop never blocks on it.
- **Overlapping children (Codex):** a request arriving while the idle child was still exiting started a new child alongside it. `check()` now publishes a `stopping` event, and a new child's first submit waits for it.
- **Indicator after an upload (both):** readiness is tracked per model (`NEEDS`: `encode_text` → siglip; `detect_and_embed` → yolo, osnet, siglip), so a text search after an upload isn't reported as loading.
- Not a defect: the reviewer said scene search doesn't start the worker. It does, because scene text goes through `encode_text` (the live scene search took 17.9 s to load).

## Live E2E (real `photofinder serve` on the full index, port 8001, run alone)

Parent at start 1328 MB (`running: false`). Text search: HTTP 200 in 19.6 s; `/api/models` reported `loading: encode_text` mid-load. Upload of a race photo: HTTP 200 in 24.8 s, 1 box (YOLO load 22 s). Worker child while loaded: 1945 MB; parent 825–1062 MB. The idle stop fired at 300 s ("stopping the model worker process") and the child was gone; parent 766 MB. On SIGTERM the server exited and the worker exited with it. On SIGKILL the child exits via `watch_parent` (test; removing the watchdog makes it fail).

## Rejected

- In-process `models.unload()` on an idle timer (the first version, commits f83ad42…511db2b): works once, then ratchets as measured above.
- `malloc_zone_pressure_relief` after unload: no effect (544 → 544 MB).

## Deferred

- The load peak is still ~3.5 GB (fp32 checkpoint + model on the CPU before the move to MPS). The child makes it transient; it doesn't lower it. Possible fix: build on the `meta` device and `load_state_dict(assign=True)` from a converted fp16 safetensors file.
- A 503 "try again" after an OOM-killed child re-runs the same ~3.5 GB load peak with no backoff.
- Pre-existing: searching before the page has loaded the profile list sends `profile_id: null` → 400 "profile_id: Input should be a valid integer".

## Tests

`tests/test_model_memory.py`: idle stop/restart, in-flight protection, idle-clock restart, loading status, a real spawned child that exits when idle, a child killed mid-request or while idle, the 503 path, and server wiring through TestClient. The real SigLIP2 fp16 test is opt-in (`PHOTOFINDER_REAL_MODELS=1`, ~2 GB). A mutation battery broke 8 of 8 key lines, and each was caught by a test.
