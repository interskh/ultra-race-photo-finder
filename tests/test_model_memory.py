import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photofinder import config, models
from photofinder.web import app as web
from test_search import X
from test_web import FakeModels, ME, upload_index


@pytest.fixture(autouse=True)
def fake_loaded(monkeypatch):
    monkeypatch.setattr(models, "_loaded", {"device": "cpu"})
    monkeypatch.setattr(models, "loading", None)


def load_fake():
    models._loaded["siglip"] = object()


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_models_unload_after_idle():
    load_fake()
    u = web.IdleUnloader(ThreadPoolExecutor(1), 0.1)
    u.begin()
    u.end()
    assert models.loaded() == ["siglip"]
    assert wait_until(lambda: models.loaded() == [])


def test_models_stay_loaded_while_work_is_in_flight():
    load_fake()
    u = web.IdleUnloader(ThreadPoolExecutor(1), 0.05)
    u.begin()
    u.begin()
    u.end()
    time.sleep(0.3)
    assert models.loaded() == ["siglip"]
    u.end()
    assert wait_until(lambda: models.loaded() == [])


def test_new_work_restarts_the_idle_clock():
    load_fake()
    u = web.IdleUnloader(ThreadPoolExecutor(1), 0.4)
    u.begin()
    u.end()
    for _ in range(4):
        time.sleep(0.2)
        u.begin()
        u.end()
        assert models.loaded() == ["siglip"]
    assert wait_until(lambda: models.loaded() == [])


def test_server_unloads_the_text_model_after_idle_and_reloads_on_demand(tmp_path, monkeypatch):
    c, _, _ = upload_index(tmp_path)
    fake = FakeModels(monkeypatch, texts={"red": X})
    encode = fake.encode

    def encode_loading(texts):
        load_fake()
        return encode(texts)
    monkeypatch.setattr(models, "encode_text", encode_loading)
    app = TestClient(web.create_app(c, idle_unload=0.2))
    for _ in range(2):
        assert app.post("/api/search", json={"profile_id": ME, "text": "red"}).status_code == 200
        assert app.get("/api/models").json()["loaded"] == ["siglip"]
        assert wait_until(lambda: app.get("/api/models").json()["loaded"] == [])
    assert len(fake.encoded) == 2


def test_model_status_reports_what_is_loading(tmp_path, monkeypatch):
    c, _, _ = upload_index(tmp_path)
    app = TestClient(web.create_app(c))
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(config, "setup_model_env", lambda: None)

    def slow_load():
        started.set()
        release.wait(5)
        return "model"
    t = threading.Thread(target=models._get, args=("siglip", slow_load))
    t.start()
    assert started.wait(5)
    assert app.get("/api/models").json() == {"loaded": [], "loading": "siglip", "unload_after_s": web.IDLE_UNLOAD}
    release.set()
    t.join(5)
    assert app.get("/api/models").json()["loading"] is None
    assert app.get("/api/models").json()["loaded"] == ["siglip"]


def test_failed_load_clears_loading(monkeypatch):
    monkeypatch.setattr(config, "setup_model_env", lambda: None)

    def broken():
        raise OSError("weights missing")
    with pytest.raises(OSError):
        models._get("siglip", broken)
    assert models.loading is None and models.loaded() == []


@pytest.mark.skipif(config.device() != "mps", reason="half precision is only used on Apple GPUs")
def test_half_precision_siglip_embeds_images_and_text(monkeypatch):
    monkeypatch.setattr(models, "_loaded", {})
    monkeypatch.setattr(models, "half_precision", True)
    try:
        model, _ = models.siglip()
        assert next(model.parameters()).dtype.itemsize == 2
        img = Image.new("RGB", (160, 320), (200, 30, 30))
        image = models.embed_images([img])
        texts = models.encode_text(["a red picture", "a green picture"])
        assert np.isfinite(image).all() and np.isfinite(texts).all()
        assert np.allclose(np.linalg.norm(image, axis=1), 1, atol=1e-3)
        red, green = texts @ image[0]
        assert red > green
    finally:
        models.unload()
