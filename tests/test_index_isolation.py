import logging
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing

import numpy as np
import pytest
from PIL import Image

from photofinder import cli, db, models
from photofinder.index import stages
from photofinder.memory import AdaptiveBatcher

DONE = {"pending": 3, "photos": 3}
OVER = "process memory footprint 2100 MB exceeds 2048 MB; stopping"


def progress(batches, items, pending):
    return {"batches": batches, "items": items, "pending": pending}


def scripted(monkeypatch, results):
    calls = []

    def fake(name, collection, max_mb):
        calls.append((name, max_mb))
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(stages, "run", fake)
    return calls


def test_stage_restarts_in_a_fresh_process_until_done(tmp_path, monkeypatch, caplog):
    calls = scripted(monkeypatch, [(None, OVER, progress(3, 40, 100)),
                                   (None, OVER, progress(2, 35, 60)),
                                   (DONE, None, progress(1, 3, 25))])
    with caplog.at_level(logging.INFO):
        assert cli.run_model_stage("detect", tmp_path, 2048) == DONE
    assert calls == [("detect", 2048)] * 3
    restarts = [r.getMessage() for r in caplog.records if "restarting in a fresh process" in r.getMessage()]
    assert len(restarts) == 2 and "detect: 40 done" in restarts[0]


def test_stage_stops_when_the_first_batch_alone_exceeds_the_limit(tmp_path, monkeypatch):
    calls = scripted(monkeypatch, [(None, "process memory footprint 1500 MB exceeds 1024 MB; stopping",
                                    progress(1, 64, 500))])
    with pytest.raises(SystemExit) as e:
        cli.run_model_stage("embed_persons", tmp_path, 1024)
    assert "raise --max-memory" in str(e.value.code) and "1024" in str(e.value.code)
    assert len(calls) == 1


def test_small_batches_under_memory_pressure_still_restart(tmp_path, monkeypatch):
    calls = scripted(monkeypatch, [(None, OVER, progress(2, 48, 500)), (DONE, None, progress(8, 452, 452))])
    assert cli.run_model_stage("embed_persons", tmp_path, 2048) == DONE
    assert len(calls) == 2


def test_restart_that_saved_nothing_stops_instead_of_looping(tmp_path, monkeypatch):
    calls = scripted(monkeypatch, [(None, OVER, progress(2, 128, 300)), (None, OVER, progress(2, 128, 300)),
                                   (DONE, None, progress(1, 1, 1))])
    with pytest.raises(SystemExit) as e:
        cli.run_model_stage("ocr_bibs", tmp_path, 2048)
    assert "saved nothing" in str(e.value.code) and len(calls) == 2


def test_a_killed_indexing_process_stops_with_a_resume_hint(tmp_path, monkeypatch):
    scripted(monkeypatch, [BrokenProcessPool("killed")])
    with pytest.raises(SystemExit) as e:
        cli.run_model_stage("embed_persons", tmp_path, 2048)
    assert "rerun to resume" in str(e.value.code)


def test_index_passes_max_memory_to_every_model_stage(tmp_path, monkeypatch):
    c = tmp_path / "coll"
    c.mkdir()
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    calls = scripted(monkeypatch, [(DONE, None, progress(1, 3, 3))] * 4)
    cli.main(["index", str(c), "--ocr", "--max-memory", "1500"])
    assert calls == [(n, 1500) for n in ("detect", "embed_persons", "embed_scenes", "ocr_bibs")]


def test_default_limit_is_4gb(tmp_path, monkeypatch):
    c = tmp_path / "coll"
    c.mkdir()
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    calls = scripted(monkeypatch, [(DONE, None, progress(1, 3, 3))] * 3)
    cli.main(["index", str(c)])
    assert {m for _, m in calls} == {4096}


def test_batcher_counts_items_handed_out(monkeypatch):
    b = AdaptiveBatcher(4, reader=lambda: 1, footprint=lambda: 100)
    assert [len(c) for c in b.chunks(range(10))] == [4, 4, 2] and b.items == 10 and b.pending == 10


def test_restarts_resume_from_saved_progress_with_a_real_stage(tmp_path, monkeypatch, caplog):
    c = tmp_path / "coll"
    c.mkdir()
    for i in range(6):
        Image.new("RGB", (300 + i, 200), "green").save(c / f"{i}.jpg", "JPEG")
    with closing(db.connect(c)) as conn:
        stages.scan(conn, c)
    embedded, text_tower_seen = [], []

    def embed(images):
        embedded.extend(img.size[0] for img in images)
        text_tower_seen.append(models.text_tower)
        return np.ones((len(images), 768), dtype=np.float32)
    monkeypatch.setattr(models, "embed_images", embed)

    def batcher(size, max_footprint_mb):
        readings = iter([0, 0] + [99999] * 10)
        return AdaptiveBatcher(2, reader=lambda: 1, footprint=lambda: next(readings),
                               max_footprint_mb=max_footprint_mb)
    monkeypatch.setattr(stages, "AdaptiveBatcher", batcher)
    with caplog.at_level(logging.INFO):
        counts = cli.run_model_stage("embed_scenes", c, 2048)
    assert counts == {"pending": 2, "photos": 2, "errors": 0}
    assert sorted(embedded) == list(range(300, 306))
    assert sum("restarting in a fresh process" in r.getMessage() for r in caplog.records) == 1
    with closing(db.connect(c)) as conn:
        assert conn.execute("select count(scene_done_at) from photos").fetchone() == (6,)
        assert conn.execute("select count(*) from emb_scene_siglip").fetchone() == (6,)
    assert text_tower_seen == [False] * 3 and models.text_tower is True


def test_stage_runs_in_a_spawned_child_process(tmp_path, monkeypatch):
    c = tmp_path / "coll"
    c.mkdir()
    with closing(db.connect(c)):
        pass
    monkeypatch.setitem(stages.MODEL_STAGES, "embed_scenes", (lambda *a, **k: {"ran": "in the parent"}, 16))
    counts, exceeded, done = cli.run_stage_isolated("embed_scenes", c, 2048)
    assert counts == {"pending": 0, "photos": 0, "errors": 0} and exceeded is None and done["items"] == 0
