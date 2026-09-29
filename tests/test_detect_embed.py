import logging
import sqlite3
import weakref

import numpy as np
import pytest
from PIL import Image

from photofinder import cli, db, models
from photofinder.index.stages import detect, embed_persons, embed_scenes, scan
from photofinder.memory import AdaptiveBatcher

BOXES = {
    (300, 200): [(10.0, 20.0, 60.0, 140.0, 0.9), (100.0, 10.0, 150.0, 190.0, 0.8)],
    (301, 200): [],
    (302, 200): [(0.0, 0.0, 40.0, 120.0, 0.7)],
}


def jpeg(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "green").save(path, "JPEG")


def truncated_jpeg(path):
    noise = np.random.default_rng(0).integers(0, 255, (200, 300, 3), dtype=np.uint8)
    Image.fromarray(noise).save(path, "JPEG", quality=95)
    data = path.read_bytes()
    path.write_bytes(data[:len(data) // 2])


def make_collection(tmp_path):
    c = tmp_path / "coll"
    for i, size in enumerate(BOXES, 1):
        jpeg(c / f"{i}.jpg", size)
    conn = db.connect(c)
    scan(conn, c)
    return c, conn


class FakeDetector:
    def __init__(self, fail_on_call=None):
        self.calls = []
        self.fail_on_call = fail_on_call

    def __call__(self, images):
        self.calls.append([img.size for img in images])
        if len(self.calls) == self.fail_on_call:
            raise RuntimeError("interrupted")
        return [BOXES[img.size] for img in images]


def fixed_batcher(size):
    return AdaptiveBatcher(size, reader=lambda: 1, sleep=lambda s: None)


def persons_by_photo(conn):
    return dict(conn.execute("""select ph.relpath, count(p.id) from photos ph
                                left join persons p on p.photo_id = ph.id group by ph.id"""))


def test_filter_keeps_confident_tall_persons_only():
    rows = [
        (0, 0, 10, 96, 0.35, 0),
        (0, 0, 10, 95.9, 0.9, 0),
        (0, 0, 10, 200, 0.34, 0),
        (0, 0, 10, 200, 0.9, 2),
        (5, 10, 50, 300, 0.8, 0),
    ]
    assert models.filter_boxes(rows) == [(0.0, 0.0, 10.0, 96.0, 0.35), (5.0, 10.0, 50.0, 300.0, 0.8)]


def test_detect_inserts_persons_and_marks_zero_person_photos(tmp_path):
    c, conn = make_collection(tmp_path)
    counts = detect(conn, c, detector=FakeDetector(), batcher=fixed_batcher(8))
    assert counts == {"pending": 3, "photos": 3, "persons": 3, "errors": 0}
    assert persons_by_photo(conn) == {"1.jpg": 2, "2.jpg": 0, "3.jpg": 1}
    assert conn.execute("select count(*) from photos where detected_at is null").fetchone() == (0,)
    assert conn.execute("select x1, y1, x2, y2, conf from persons where photo_id = 3").fetchall() == [
        (0.0, 0.0, 40.0, 120.0, 0.7)]


def test_detect_resumes_without_redoing_or_duplicating(tmp_path):
    c, conn = make_collection(tmp_path)
    with pytest.raises(RuntimeError):
        detect(conn, c, detector=FakeDetector(fail_on_call=2), batcher=fixed_batcher(1))
    assert persons_by_photo(conn) == {"1.jpg": 2, "2.jpg": 0, "3.jpg": 0}
    done = dict(conn.execute("select relpath, detected_at from photos"))
    assert done["1.jpg"] and done["2.jpg"] is None and done["3.jpg"] is None

    again = FakeDetector()
    detect(conn, c, detector=again, batcher=fixed_batcher(8))
    assert again.calls == [[(301, 200), (302, 200)]]
    assert persons_by_photo(conn) == {"1.jpg": 2, "2.jpg": 0, "3.jpg": 1}

    idle = FakeDetector()
    assert detect(conn, c, detector=idle)["pending"] == 0
    assert idle.calls == []


def test_detect_failure_mid_transaction_leaves_no_orphan_persons(tmp_path):
    c, conn = make_collection(tmp_path)
    conn.execute("""create trigger boom before update of detected_at on photos when new.id = 1
                    begin select raise(abort, 'boom'); end""")
    with pytest.raises(sqlite3.IntegrityError):
        detect(conn, c, detector=FakeDetector(), batcher=fixed_batcher(8))
    assert conn.execute("select count(*) from persons").fetchone() == (0,)
    conn.execute("drop trigger boom")
    detect(conn, c, detector=FakeDetector(), batcher=fixed_batcher(8))
    assert persons_by_photo(conn) == {"1.jpg": 2, "2.jpg": 0, "3.jpg": 1}


def test_truncated_image_becomes_error_row_and_stage_continues(tmp_path):
    c, conn = make_collection(tmp_path)
    truncated_jpeg(c / "0.jpg")
    scan(conn, c)
    assert conn.execute("select status from photos where relpath = '0.jpg'").fetchone() == ("ok",)

    fake = FakeDetector()
    counts = detect(conn, c, detector=fake, batcher=fixed_batcher(8))
    assert counts["errors"] == 1 and counts["photos"] == 3
    assert fake.calls == [[(300, 200), (301, 200), (302, 200)]]
    status, error, detected = conn.execute(
        "select status, error, detected_at from photos where relpath = '0.jpg'").fetchone()
    assert status == "error" and "truncated" in error and detected is None
    assert detect(conn, c, detector=FakeDetector())["pending"] == 0


def test_all_unreadable_batch_never_calls_detector(tmp_path):
    c = tmp_path / "coll"
    c.mkdir()
    truncated_jpeg(c / "0.jpg")
    conn = db.connect(c)
    scan(conn, c)
    fake = FakeDetector()
    assert detect(conn, c, detector=fake)["errors"] == 1
    assert fake.calls == []


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def __call__(self, crops):
        self.calls.append([c.size for c in crops])
        n = len(crops)
        reid = np.arange(1, 513, dtype=np.float32) * np.arange(1, n + 1)[:, None]
        clip = np.full((n, 768), 3.0, dtype=np.float32)
        return reid, clip


def vectors(conn, table):
    return {pid: np.frombuffer(v, dtype=np.float16) for pid, v in conn.execute(f"select person_id, v from {table}")}


def test_embed_stores_normalized_float16_and_skips_done(tmp_path):
    c, conn = make_collection(tmp_path)
    detect(conn, c, detector=FakeDetector(), batcher=fixed_batcher(8))
    fake = FakeEmbedder()
    counts = embed_persons(conn, c, embedder=fake, batcher=fixed_batcher(2))
    assert counts == {"pending": 3, "persons": 3, "errors": 0}
    assert fake.calls == [[(50, 120), (50, 180)], [(40, 120)]]

    osnet, siglip = vectors(conn, "emb_person_osnet"), vectors(conn, "emb_person_siglip")
    assert set(osnet) == set(siglip) == {1, 2, 3}
    for v in osnet.values():
        assert v.shape == (512,)
        assert np.linalg.norm(v.astype(np.float32)) == pytest.approx(1, abs=1e-3)
        assert v[-1] / v[0] == pytest.approx(512, rel=1e-2)
    for v in siglip.values():
        assert v.shape == (768,)
        assert np.allclose(v.astype(np.float32), 1 / np.sqrt(768), atol=1e-3)
    assert conn.execute("select count(*) from persons where embedded_at is null").fetchone() == (0,)

    conn.execute("insert into persons(photo_id, x1, y1, x2, y2, conf) values (3, 10, 10, 30, 150, 0.9)")
    conn.commit()
    again = FakeEmbedder()
    assert embed_persons(conn, c, embedder=again)["persons"] == 1
    assert again.calls == [[(20, 140)]]


def test_embed_unreadable_photo_marks_error_and_skips_its_persons(tmp_path):
    c, conn = make_collection(tmp_path)
    detect(conn, c, detector=FakeDetector(), batcher=fixed_batcher(8))
    (c / "1.jpg").write_bytes(b"gone bad")
    fake = FakeEmbedder()
    counts = embed_persons(conn, c, embedder=fake, batcher=fixed_batcher(8))
    assert counts == {"pending": 3, "persons": 1, "errors": 1}
    assert fake.calls == [[(40, 120)]]
    assert conn.execute("select status from photos where relpath = '1.jpg'").fetchone() == ("error",)
    assert embed_persons(conn, c, embedder=FakeEmbedder())["pending"] == 0


def track_decoded_photos(monkeypatch):
    refs, peak = [], [0]
    load = models.load_image

    def alive():
        return sum(r() is not None for r in refs)

    def tracked(path):
        img = load(path)
        refs.append(weakref.ref(img))
        peak[0] = max(peak[0], alive())
        return img
    monkeypatch.setattr(models, "load_image", tracked)
    return alive, peak


def test_embed_persons_decodes_one_photo_at_a_time(tmp_path, monkeypatch):
    c, conn = make_collection(tmp_path)
    detect(conn, c, detector=FakeDetector(), batcher=fixed_batcher(8))
    alive, peak = track_decoded_photos(monkeypatch)
    fake, held = FakeEmbedder(), []

    def embedder(crops):
        held.append(alive())
        return fake(crops)
    assert embed_persons(conn, c, embedder=embedder, batcher=fixed_batcher(8))["persons"] == 3
    assert fake.calls == [[(50, 120), (50, 180), (40, 120)]]
    assert peak == [1] and held == [0]


def test_embed_crops_runs_osnet_in_sub_batches(monkeypatch):
    sizes = []

    def reid(arrays):
        sizes.append(len(arrays))
        return np.array([[a.shape[1], 1.0] for a in arrays], dtype=np.float32)
    monkeypatch.setattr(models, "osnet", lambda: reid)
    monkeypatch.setattr(models, "embed_images", lambda crops: np.ones((len(crops), 3), dtype=np.float32))
    crops = [Image.new("RGB", (i + 1, 5)) for i in range(70)]
    osnet, clip = models.embed_crops(crops)
    assert sizes == [32, 32, 6] and clip.shape == (70, 3)
    assert np.allclose(osnet, models.l2norm([[i + 1, 1.0] for i in range(70)]))


def test_index_rerun_with_everything_done_loads_no_model(tmp_path, monkeypatch, caplog):
    c, conn = make_collection(tmp_path)
    conn.close()
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    monkeypatch.setattr(models, "detect_persons", FakeDetector())
    monkeypatch.setattr(models, "embed_crops", FakeEmbedder())
    monkeypatch.setattr(models, "embed_images", FakeSceneEmbedder())
    monkeypatch.setattr(models, "read_text", lambda img: [])
    cli.main(["index", str(c), "--ocr"])
    monkeypatch.undo()

    def forbidden(*args):
        raise AssertionError("model loaded")

    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    for name in ("yolo", "osnet", "siglip", "read_text"):
        monkeypatch.setattr(models, name, forbidden)
    with caplog.at_level(logging.INFO):
        cli.main(["index", str(c), "--ocr"])
    messages = [r.getMessage() for r in caplog.records]
    assert "detect: 0 pending" in messages and "embed_persons: 0 pending" in messages
    assert "embed_scenes: 0 pending" in messages and "ocr_bibs: 0 pending" in messages
    conn = sqlite3.connect(c / "index.sqlite")
    assert conn.execute("select count(*) from emb_person_osnet").fetchone() == (3,)
    assert conn.execute("select count(*) from emb_scene_siglip").fetchone() == (3,)


class FakeSceneEmbedder:
    def __init__(self, fail_on_call=None):
        self.calls = []
        self.fail_on_call = fail_on_call

    def __call__(self, images):
        self.calls.append([img.size for img in images])
        if len(self.calls) == self.fail_on_call:
            raise RuntimeError("interrupted")
        return np.array([[img.width, img.height] + [1.0] * 766 for img in images], dtype=np.float32)


def scene_vectors(conn):
    return {relpath: np.frombuffer(v, dtype=np.float16).astype(np.float32) for relpath, v in conn.execute(
        "select ph.relpath, e.v from emb_scene_siglip e join photos ph on ph.id = e.photo_id")}


def test_embed_scenes_stores_whole_image_vectors_and_skips_done(tmp_path):
    c, conn = make_collection(tmp_path)
    fake = FakeSceneEmbedder()
    counts = embed_scenes(conn, c, embedder=fake, batcher=fixed_batcher(2))
    assert counts == {"pending": 3, "photos": 3, "errors": 0}
    assert fake.calls == [[(300, 200), (301, 200)], [(302, 200)]]
    vecs = scene_vectors(conn)
    assert set(vecs) == {"1.jpg", "2.jpg", "3.jpg"}
    for relpath, v in vecs.items():
        assert v.shape == (768,)
        assert np.linalg.norm(v) == pytest.approx(1, abs=1e-3)
    assert vecs["2.jpg"][0] / vecs["2.jpg"][2] == pytest.approx(301, rel=1e-2)
    assert conn.execute("select count(*) from photos where scene_done_at is null").fetchone() == (0,)

    idle = FakeSceneEmbedder()
    assert embed_scenes(conn, c, embedder=idle)["pending"] == 0
    assert idle.calls == []

    jpeg(c / "4.jpg", (303, 200))
    scan(conn, c)
    again = FakeSceneEmbedder()
    assert embed_scenes(conn, c, embedder=again)["photos"] == 1
    assert again.calls == [[(303, 200)]]


def test_embed_scenes_uses_exif_upright_image(tmp_path):
    c = tmp_path / "coll"
    c.mkdir()
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (300, 200), "green").save(c / "r.jpg", "JPEG", exif=exif)
    conn = db.connect(c)
    scan(conn, c)
    fake = FakeSceneEmbedder()
    embed_scenes(conn, c, embedder=fake)
    assert fake.calls == [[(200, 300)]]


def test_embed_scenes_resumes_after_crash_without_duplicates(tmp_path):
    c, conn = make_collection(tmp_path)
    with pytest.raises(RuntimeError):
        embed_scenes(conn, c, embedder=FakeSceneEmbedder(fail_on_call=2), batcher=fixed_batcher(1))
    assert set(scene_vectors(conn)) == {"1.jpg"}
    again = FakeSceneEmbedder()
    embed_scenes(conn, c, embedder=again, batcher=fixed_batcher(8))
    assert again.calls == [[(301, 200), (302, 200)]]
    assert conn.execute("select count(*) from emb_scene_siglip").fetchone() == (3,)


def test_embed_scenes_failure_mid_transaction_rolls_back_batch(tmp_path):
    c, conn = make_collection(tmp_path)
    conn.execute("""create trigger boom before update of scene_done_at on photos when new.id = 2
                    begin select raise(abort, 'boom'); end""")
    with pytest.raises(sqlite3.IntegrityError):
        embed_scenes(conn, c, embedder=FakeSceneEmbedder(), batcher=fixed_batcher(8))
    assert conn.execute("select count(*) from emb_scene_siglip").fetchone() == (0,)
    assert conn.execute("select count(*) from photos where scene_done_at is not null").fetchone() == (0,)
    conn.execute("drop trigger boom")
    assert embed_scenes(conn, c, embedder=FakeSceneEmbedder())["photos"] == 3
    assert conn.execute("select count(*) from emb_scene_siglip").fetchone() == (3,)


def test_embed_scenes_unreadable_photo_marks_error(tmp_path):
    c, conn = make_collection(tmp_path)
    (c / "1.jpg").write_bytes(b"gone bad")
    fake = FakeSceneEmbedder()
    counts = embed_scenes(conn, c, embedder=fake, batcher=fixed_batcher(8))
    assert counts == {"pending": 3, "photos": 2, "errors": 1}
    assert fake.calls == [[(301, 200), (302, 200)]]
    status, error, done = conn.execute("select status, error, scene_done_at from photos where relpath = '1.jpg'").fetchone()
    assert status == "error" and error and done is None
    assert set(scene_vectors(conn)) == {"2.jpg", "3.jpg"}
    assert embed_scenes(conn, c, embedder=FakeSceneEmbedder())["pending"] == 0


def test_index_skips_bib_ocr_unless_asked(tmp_path, monkeypatch, caplog):
    c, conn = make_collection(tmp_path)
    conn.close()
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    monkeypatch.setattr(models, "detect_persons", FakeDetector())
    monkeypatch.setattr(models, "embed_crops", FakeEmbedder())
    monkeypatch.setattr(models, "embed_images", FakeSceneEmbedder())

    def forbidden(img):
        raise AssertionError("OCR ran without --ocr")
    monkeypatch.setattr(models, "read_text", forbidden)
    with caplog.at_level(logging.INFO):
        cli.main(["index", str(c)])
    assert not any("ocr_bibs" in r.getMessage() for r in caplog.records)
    conn = sqlite3.connect(c / "index.sqlite")
    assert conn.execute("select count(*) from persons where embedded_at is not null").fetchone()[0] > 0
    assert conn.execute("select count(*) from persons where ocr_at is not null").fetchone()[0] == 0
