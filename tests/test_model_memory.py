import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photofinder import config, models
from photofinder.web import app as web
from test_search import X
from test_web import ME, FakeModels, no_real_models, upload_index  # noqa: F401

REAL_POOL = web.model_pool


class Pools:
    def __init__(self):
        self.made = []

    def __call__(self):
        pool = ThreadPoolExecutor(1)
        self.made.append(pool)
        return pool

    def live(self):
        return [p for p in self.made if not p._shutdown]


def wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_worker_stops_after_idle_and_restarts_on_demand():
    pools = Pools()
    w = web.ModelWorker(0.1, pools)
    assert w.submit(sum, [1, 2]).result() == 3
    assert len(pools.live()) == 1
    assert wait_until(lambda: not pools.live())
    assert w.status()["running"] is False
    assert w.submit(sum, [3]).result() == 3
    assert len(pools.made) == 2 and len(pools.live()) == 1


def test_worker_is_not_stopped_while_work_is_in_flight():
    pools = Pools()
    w = web.ModelWorker(0.05, pools)
    w.submit(sum, [1]).result()
    release = threading.Event()
    slow = w.submit(release.wait, 5)
    time.sleep(0.4)
    assert pools.live() and not slow.done()
    release.set()
    assert slow.result() is True
    assert wait_until(lambda: not pools.live())


def test_new_work_restarts_the_idle_clock():
    pools = Pools()
    w = web.ModelWorker(1.0, pools)
    w.submit(sum, [1]).result()
    for _ in range(4):
        time.sleep(0.4)
        w.submit(sum, [1]).result()
        assert len(pools.live()) == 1
    assert len(pools.made) == 1
    assert wait_until(lambda: not pools.live())


def test_status_reports_loading_only_for_the_first_call_in_a_fresh_worker():
    w = web.ModelWorker(60, Pools())
    release = threading.Event()

    def encode_text(_):
        release.wait(5)
        return "vec"
    first = w.submit(encode_text, "a")
    assert w.status()["loading"] == "encode_text"
    release.set()
    first.result()
    assert wait_until(lambda: w.status()["ready"] == ["siglip"])
    assert w.status()["loading"] is None
    release.clear()
    again = w.submit(encode_text, "b")
    assert w.status()["loading"] is None
    release.set()
    again.result()


def test_text_search_after_an_upload_is_not_reported_as_loading():
    w = web.ModelWorker(60, Pools())

    def detect_and_embed(_):
        return "boxes"
    w.submit(detect_and_embed, "img").result()
    assert wait_until(lambda: w.status()["ready"] == ["osnet", "siglip", "yolo"])
    release = threading.Event()

    def encode_text(_):
        release.wait(5)
    fut = w.submit(encode_text, "red")
    assert w.status()["loading"] is None
    release.set()
    fut.result()


class LockedPool(ThreadPoolExecutor):
    def __init__(self):
        super().__init__(1)
        self.shutdown_lock = threading.Lock()

    def submit(self, fn, *args):
        with self.shutdown_lock:
            return super().submit(fn, *args)


def test_a_dying_worker_does_not_deadlock_a_concurrent_submit():
    pool = LockedPool()
    w = web.ModelWorker(60, lambda: pool)
    first = w.submit(sum, [1])
    first.result()
    submitted = threading.Event()

    def request():
        w.submit(sum, [2]).result()
        submitted.set()
    with pool.shutdown_lock:
        t = threading.Thread(target=request, daemon=True)
        t.start()
        time.sleep(0.2)
        callback = threading.Thread(target=w.finished, args=(pool, "sum", first), daemon=True)
        w.running.append("sum")
        callback.start()
        callback.join(2)
        assert not callback.is_alive(), "done-callback deadlocked against submit"
    assert submitted.wait(5)


class SlowStopPool(ThreadPoolExecutor):
    def __init__(self, log, release):
        super().__init__(1)
        self.log, self.release = log, release

    def submit(self, fn, *args):
        self.log.append(("submit", self))
        return super().submit(fn, *args)

    def shutdown(self, wait=True, **kw):
        self.release.wait(5)
        self.log.append(("stopped", self))
        super().shutdown(wait, **kw)


def test_a_new_worker_waits_for_the_old_one_to_exit():
    log, release = [], threading.Event()
    w = web.ModelWorker(0.05, lambda: SlowStopPool(log, release))
    w.submit(sum, [1]).result()
    assert wait_until(lambda: w.status()["running"] is False)
    new = threading.Thread(target=lambda: w.submit(sum, [2]).result(), daemon=True)
    new.start()
    time.sleep(0.3)
    assert [e for e, _ in log] == ["submit"]
    release.set()
    new.join(5)
    assert [e for e, _ in log][:3] == ["submit", "stopped", "submit"]
    assert log[0][1] is log[1][1] is not log[2][1]


def test_real_worker_runs_in_a_child_process_that_exits_when_idle():
    w = web.ModelWorker(0.5, REAL_POOL)
    child = w.submit(os.getpid).result(timeout=60)
    assert child != os.getpid() and alive(child)
    assert wait_until(lambda: not alive(child), timeout=10)
    assert w.status()["running"] is False


def test_worker_killed_mid_request_fails_that_call_and_the_next_call_starts_a_new_one():
    w = web.ModelWorker(60, REAL_POOL)
    child = w.submit(os.getpid).result(timeout=60)
    doomed = w.submit(time.sleep, 30)
    os.kill(child, signal.SIGKILL)
    with pytest.raises(BrokenProcessPool):
        doomed.result(timeout=30)
    assert wait_until(lambda: w.status()["running"] is False)
    fresh = w.submit(os.getpid).result(timeout=60)
    assert fresh != child and alive(fresh)


def test_worker_that_died_while_idle_is_replaced():
    w = web.ModelWorker(60, REAL_POOL)
    child = w.submit(os.getpid).result(timeout=60)
    os.kill(child, signal.SIGKILL)
    assert wait_until(lambda: not alive(child))
    time.sleep(0.5)
    fresh = w.submit(os.getpid).result(timeout=60)
    assert fresh != child


def test_worker_exits_when_the_server_is_killed():
    code = ("import os, sys, time\n"
            "from photofinder.web import app as web\n"
            "w = web.ModelWorker(600)\n"
            "print(w.submit(os.getpid).result(timeout=60), flush=True)\n"
            "time.sleep(60)\n")
    server = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        child = int(server.stdout.readline())
        assert alive(child)
    finally:
        server.kill()
        server.wait()
    assert wait_until(lambda: not alive(child), timeout=10)


def test_request_on_a_dying_worker_is_503(tmp_path, monkeypatch):
    c, _, _ = upload_index(tmp_path)
    FakeModels(monkeypatch, texts={"red": X})

    def broken(texts):
        raise BrokenProcessPool("child died")
    monkeypatch.setattr(models, "encode_text", broken)
    res = TestClient(web.create_app(c)).post("/api/search", json={"profile_id": ME, "text": "red"})
    assert res.status_code == 503
    assert "model worker stopped" in res.json()["detail"]


def test_server_stops_the_model_worker_after_idle_and_restarts_it(tmp_path, monkeypatch):
    c, _, _ = upload_index(tmp_path)
    fake = FakeModels(monkeypatch, texts={"red": X})
    pools = Pools()
    api = TestClient(web.create_app(c, idle_unload=0.2, worker_factory=pools))
    for i in range(2):
        assert api.post("/api/search", json={"profile_id": ME, "text": "red"}).status_code == 200
        status = api.get("/api/models").json()
        assert status["running"] is True and status["ready"] == ["encode"]
        assert wait_until(lambda: api.get("/api/models").json()["running"] is False)
        assert len(pools.made) == i + 1
    assert len(fake.encoded) == 2


@pytest.mark.skipif(not os.environ.get("PHOTOFINDER_REAL_MODELS") or config.device() != "mps",
                    reason="loads SigLIP2 (~2 GB); set PHOTOFINDER_REAL_MODELS=1 on an Apple GPU")
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
