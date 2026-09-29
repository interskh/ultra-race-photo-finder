import csv
import io
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from photofinder import config, originals
from photofinder.sources import yipai
from photofinder.web import app as web
from test_search import A, X, make_index
from test_web import RaceClient, ME, label, no_real_models  # noqa: F401
from test_yipai import FakeTime

ORDER = "ORD"


def jpeg(pid):
    return b"\xff\xd8" + f"photo {pid}".encode() + b"x" * 2000 + b"\xff\xd9"


class Gallery:
    def __init__(self, t, page_size=2):
        self.t, self.page_size = t, page_size
        self.photos, self.images = [], {}
        self.lookups, self.fetched = [], []
        self.api = lambda req: None
        self.on_image = lambda pid: None

    def add(self, pid, fname):
        self.photos.append({"photoId": pid, "orderId": ORDER, "fname": fname, "img": {
            "primary": "https://o-a.test", "failover": "https://o-b.test", "path": f"/o/{pid}", "sign": "?Expires=1"}})

    def __call__(self, req):
        if req.url.path.endswith("/audience/photos"):
            assert req.url.path == f"/api/v1/yipai/order/{ORDER}/audience/photos"
            self.lookups.append((self.t.now, req.url.params["fileName"], int(req.url.params["page"])))
            forced = self.api(req)
            if forced is not None:
                return forced
            hits = [p for p in self.photos if Path(p["fname"]).stem == req.url.params["fileName"]]
            page = int(req.url.params["page"])
            return httpx.Response(200, json={"status": 200, "data": {
                "pagination": {"page": page, "count": len(hits), "totalPage": -(-len(hits) // self.page_size)},
                "photos": hits[(page - 1) * self.page_size: page * self.page_size]}})
        pid = int(req.url.path.rsplit("/", 1)[1])
        self.fetched.append((req.url.host, pid))
        self.on_image(pid)
        replies = self.images.get(pid)
        return replies.pop(0) if replies else httpx.Response(200, content=jpeg(pid))

    def fetched_ids(self):
        return [pid for _, pid in self.fetched]


def yipai_index(tmp_path, monkeypatch, photos):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data")
    n = len(photos)
    c, conn, ids = make_index(tmp_path, [(i, (0, 0, 50, 100), A, X) for i in range(1, n + 1)], photos=n)
    conn.executemany("update photos set taken_at = ?, photographer = ?, grp = ? where relpath = ?",
                     [(t, ph, "9.25 赛事", f"{i}.jpg") for i, (_, t, ph) in enumerate(photos, 1)])
    conn.commit()
    m = sqlite3.connect(c / "manifest.sqlite")
    m.executescript(yipai.SCHEMA)
    m.executemany("insert into photos(photo_id, order_id, fname) values (?,?,?)",
                  [(i, ORDER, fname) for i, (fname, _, _) in enumerate(photos, 1)])
    m.commit()
    m.close()
    return c, conn, ids


def setup(tmp_path, monkeypatch, photos, mark=True, **kw):
    c, conn, ids = yipai_index(tmp_path, monkeypatch, photos)
    t = FakeTime()
    g = Gallery(t)
    fetcher = originals.Fetcher(httpx.Client(transport=httpx.MockTransport(g)), sleep=t.sleep, clock=t.clock, **kw)
    api = RaceClient(web.create_app(c, fetcher=fetcher))
    if mark:
        for pids in ids.values():
            label(api, pids[0], "me")
    return c, conn, ids, t, g, api


def run(api, profile=ME):
    res = api.post("/api/originals", json={"profile_id": profile})
    assert res.status_code == 200, res.text
    api.app.state.originals.thread.join(10)
    status = api.get("/api/originals").json()
    assert status["state"] != "running"
    return status


def folder(tmp_path, name="Me"):
    return tmp_path / "data" / "exports" / "coll" / name


def files(tmp_path, name="Me"):
    d = folder(tmp_path, name) / "originals"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def read_csv(tmp_path, name="Me"):
    raw = (folder(tmp_path, name) / "photos.csv").read_bytes()
    assert raw.startswith("﻿".encode())
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))


def test_duplicate_names_resolved_by_photo_id_across_pages_with_paced_lookups(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [
        ("IMG_1.JPG", "2026-09-25 10:00:00", "阿光 A/B"),
        ("未标题-1.jpg", "2026-09-25 09:00:00", None),
    ])
    for pid, fname in ((101, "IMG_1.JPG"), (102, "IMG_1.JPG"), (1, "IMG_1.JPG"), (201, "未标题-1.jpg"),
                       (2, "未标题-1.jpg")):
        g.add(pid, fname)
    status = run(api)
    assert status["state"] == "done" and (status["done"], status["total"]) == (2, 2)
    assert status["counts"] == {"downloaded": 2, "skipped": 0, "buy_on_site": 0, "failed": 0}
    assert status["folder"] == str(folder(tmp_path)) and status["profile"] == "Me"
    assert [(f, p) for _, f, p in g.lookups] == [("未标题-1", 1), ("IMG_1", 1), ("IMG_1", 2)]
    times = [when for when, _, _ in g.lookups]
    assert all(b - a >= originals.LOOKUP_GAP for a, b in zip(times, times[1:]))
    assert files(tmp_path) == ["20260925-090000_unknown_2.jpg", "20260925-100000_阿光_A_B_1.jpg"]
    d = folder(tmp_path) / "originals"
    assert (d / "20260925-100000_阿光_A_B_1.jpg").read_bytes() == jpeg(1)
    assert (d / "20260925-090000_unknown_2.jpg").read_bytes() == jpeg(2)
    assert sorted(g.fetched_ids()) == [1, 2]


def test_403_non_jpeg_and_missing_are_buy_on_site_and_job_continues(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [
        ("A1.JPG", "2026-09-25 08:01:00", "cam"),
        ("A2.JPG", "2026-09-25 08:02:00", "cam"),
        ("A3.JPG", "2026-09-25 08:03:00", "cam"),
        ("A4.JPG", "2026-09-25 08:04:00", "cam"),
        ("A5.JPG", "2026-09-25 08:05:00", "cam"),
        ("A6.JPG", None, "cam"),
    ], tries=2)
    for pid in (1, 2, 4, 5, 6):
        g.add(pid, f"A{pid}.JPG")
    g.images = {1: [httpx.Response(403)], 2: [httpx.Response(200, content=b"<html>buy</html>" * 100)],
                4: [httpx.Response(503, headers={"retry-after": "30"}), httpx.Response(200, content=jpeg(4))],
                5: [httpx.Response(200, content=jpeg(5)[:-2])] * 2}
    status = run(api)
    assert status["state"] == "done" and status["done"] == 6
    assert status["counts"] == {"downloaded": 2, "skipped": 0, "buy_on_site": 3, "failed": 1}
    assert [e.split(": ", 1)[1] for e in status["errors"]] == [
        "buy on site: HTTP 403", "buy on site: not a JPEG", "buy on site: not found in the gallery",
        "failed: truncated JPEG"]
    assert g.fetched == [("o-a.test", 1), ("o-a.test", 2), ("o-a.test", 4), ("o-b.test", 4), ("o-a.test", 5),
                         ("o-b.test", 5), ("o-a.test", 6)]
    assert 30 in t.slept
    assert files(tmp_path) == ["20260925-080400_cam_4.jpg", "undated_cam_6.jpg"]
    rows = read_csv(tmp_path)
    assert rows[0] == originals.COLUMNS
    assert [(r[0], r[1], r[8]) for r in rows[1:]] == [
        ("1", "A1.JPG", "buy on site: HTTP 403"), ("2", "A2.JPG", "buy on site: not a JPEG"),
        ("3", "A3.JPG", "buy on site: not found in the gallery"), ("4", "A4.JPG", "downloaded"),
        ("5", "A5.JPG", "failed: truncated JPEG"), ("6", "A6.JPG", "downloaded")]
    assert rows[4][7] == str(folder(tmp_path) / "originals" / "20260925-080400_cam_4.jpg") and rows[1][7] == ""
    assert rows[4][6] == str(c.resolve() / "4.jpg") and rows[4][2:6] == ["cam", "2026-09-25 08:04:00", "", "9.25 赛事"]

    restarted = RaceClient(web.create_app(c))
    mine = restarted.get("/api/me", params={"profile_id": ME}).json()["photos"]
    assert [p["original"] for p in mine] == [r[8] for r in rows[1:]]
    (folder(tmp_path) / "originals" / "undated_cam_6.jpg").unlink()
    mine = restarted.get("/api/me", params={"profile_id": ME}).json()["photos"]
    assert mine[5]["original"] is None


def test_rerun_skips_existing_without_requests_and_refetches_stale_part(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [
        ("A1.JPG", "2026-09-25 08:01:00", "cam"), ("A2.JPG", "2026-09-25 08:02:00", "cam")])
    g.add(1, "A1.JPG")
    g.add(2, "A2.JPG")
    run(api)
    g.lookups.clear(), g.fetched.clear()
    status = run(api)
    assert (g.lookups, g.fetched) == ([], [])
    assert status["counts"] == {"downloaded": 0, "skipped": 2, "buy_on_site": 0, "failed": 0}
    d = folder(tmp_path) / "originals"
    (d / "20260925-080200_cam_2.jpg").rename(d / "20260925-080200_cam_2.jpg.part")
    (d / "20260925-080100_cam_1.jpg").write_bytes(jpeg(1)[:-2])
    status = run(api)
    assert sorted(g.fetched_ids()) == [1, 2]
    assert status["counts"]["downloaded"] == 2
    assert files(tmp_path) == ["20260925-080100_cam_1.jpg", "20260925-080200_cam_2.jpg"]
    assert (d / "20260925-080100_cam_1.jpg").read_bytes() == jpeg(1)


def test_cancel_mid_run_leaves_remaining_photos_untouched(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [
        (f"A{i}.JPG", f"2026-09-25 08:0{i}:00", "cam") for i in range(1, 4)])
    for i in range(1, 4):
        g.add(i, f"A{i}.JPG")
    g.on_image = lambda pid: api.app.state.originals.cancel()
    status = run(api)
    assert status["state"] == "cancelled" and status["done"] == 1 and status["current"] is None
    assert g.fetched_ids() == [1] and [f for _, f, _ in g.lookups] == ["A1"]
    assert files(tmp_path) == ["20260925-080100_cam_1.jpg"]
    assert [r[8] for r in read_csv(tmp_path)[1:]] == ["downloaded", "", ""]


def test_cancel_interrupts_pacing_wait_and_blocks_other_downloads(tmp_path, monkeypatch):
    c, conn, ids = yipai_index(tmp_path, monkeypatch, [("A1.JPG", "2026-09-25 08:01:00", "cam"),
                                                       ("A2.JPG", "2026-09-25 08:02:00", "cam")])
    g = Gallery(FakeTime())
    g.add(1, "A1.JPG")
    looked = threading.Event()
    g.api = lambda req: looked.set()
    fetcher = originals.Fetcher(httpx.Client(transport=httpx.MockTransport(g)), img_delay=(60, 60))
    api = RaceClient(web.create_app(c, fetcher=fetcher))
    for pids in ids.values():
        label(api, pids[0], "me")
    ann = api.post("/api/profiles", json={"name": "Ann"}).json()["id"]
    assert api.post("/api/originals", json={"profile_id": ME}).status_code == 200
    assert looked.wait(5)
    res = api.post("/api/originals", json={"profile_id": ME})
    assert res.status_code == 409 and "already running" in res.json()["detail"]
    photo = conn.execute("select id from photos where relpath = '2.jpg'").fetchone()[0]
    assert api.post(f"/api/photos/{photo}/original", json={"profile_id": ME}).status_code == 409
    assert not folder(tmp_path).exists()
    assert api.patch(f"/api/profiles/{ME}", json={"name": "Bob"}).status_code == 409
    assert api.delete(f"/api/profiles/{ME}").status_code == 409
    (folder(tmp_path, "Ann") / "originals").mkdir(parents=True)
    res = api.patch(f"/api/profiles/{ann}", json={"name": "Dee"})
    assert res.status_code == 409 and "rename when it finishes" in res.json()["detail"]
    assert folder(tmp_path, "Ann").exists() and not folder(tmp_path, "Dee").exists()
    assert [p["name"] for p in api.get("/api/profiles").json()["profiles"]] == ["Me", "Ann"]
    t0 = time.monotonic()
    assert api.post("/api/originals/cancel").json()["state"] in ("running", "cancelled")
    api.app.state.originals.thread.join(5)
    assert time.monotonic() - t0 < 5
    assert api.get("/api/originals").json()["state"] == "cancelled"
    assert g.fetched == [] and len(g.lookups) == 1


def test_api_failure_stops_job_with_error(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [
        ("A1.JPG", "2026-09-25 08:01:00", "cam"), ("A2.JPG", "2026-09-25 08:02:00", "cam")])
    g.api = lambda req: httpx.Response(401)
    status = run(api)
    assert status["state"] == "error" and status["done"] == 0
    assert "yipai360 API unavailable" in status["errors"][-1]
    assert len(g.lookups) == 1 and g.fetched == []
    assert [r[8] for r in read_csv(tmp_path)[1:]] == ["", ""]


def test_lookups_stay_under_the_sites_rate_limit():
    assert 60 / originals.LOOKUP_GAP <= 10


def test_rate_limited_lookups_wait_it_out_and_finish(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [
        (f"A{i}.JPG", f"2026-09-25 08:0{i}:00", "cam") for i in range(1, 4)])
    for i in range(1, 4):
        g.add(i, f"A{i}.JPG")
    block = {}

    def rate_limit(req):
        if len(g.lookups) == 2:
            block["until"] = t.now + 100
        if block and t.now < block["until"]:
            return httpx.Response(500)
    g.api = rate_limit
    status = run(api)
    assert status["state"] == "done" and status["counts"]["downloaded"] == 3
    assert originals.RATE_LIMIT_WAITS[0] in t.slept
    assert sorted(g.fetched_ids()) == [1, 2, 3]


def test_persistent_rate_limit_gives_up_after_the_long_waits(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", "2026-09-25 08:01:00", "cam")])
    g.add(1, "A1.JPG")
    g.api = lambda req: httpx.Response(500)
    status = run(api)
    assert status["state"] == "error" and "yipai360 API unavailable" in status["errors"][-1]
    assert all(w in t.slept for w in originals.RATE_LIMIT_WAITS)
    assert g.fetched == []


def test_lookup_stops_paging_after_the_page_cap(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", None, "cam")])
    g.page_size = 1
    for pid in range(100, 100 + originals.MAX_LOOKUP_PAGES):
        g.add(pid, "A1.JPG")
    g.add(1, "A1.JPG")
    status = run(api)
    assert status["errors"] == [f"1 A1.JPG: failed: gallery search for A1.JPG has more than "
                                f"{originals.MAX_LOOKUP_PAGES} pages"]
    assert len(g.lookups) == originals.MAX_LOOKUP_PAGES and g.fetched == []


def test_start_needs_marked_photos(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", None, "cam")], mark=False)
    res = api.post("/api/originals", json={"profile_id": ME})
    assert res.status_code == 400 and "marked" in res.json()["detail"]
    assert api.post("/api/originals", json={"profile_id": 9}).status_code == 404
    assert api.get("/api/originals").json() == {
        "state": "idle", "profile_id": None, "profile": None, "done": 0, "total": 0, "current": None,
        "counts": {"downloaded": 0, "skipped": 0, "buy_on_site": 0, "failed": 0}, "errors": [], "folder": None}


def test_zip_contains_originals_and_csv(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [
        ("A1.JPG", "2026-09-25 08:01:00", "cam"), ("A2.JPG", "2026-09-25 08:02:00", "cam")])
    g.add(1, "A1.JPG")
    g.add(2, "A2.JPG")
    ann = api.post("/api/profiles", json={"name": "Ann"}).json()["id"]
    assert api.get("/api/originals/zip", params={"profile_id": ME}).status_code == 404
    run(api)
    res = api.get("/api/originals/zip", params={"profile_id": ME})
    assert res.status_code == 200 and res.headers["content-type"] == "application/zip"
    assert 'filename="coll-Me-originals.zip"' in res.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(res.content))
    assert sorted(z.namelist()) == ["originals/20260925-080100_cam_1.jpg", "originals/20260925-080200_cam_2.jpg",
                                    "photos.csv"]
    assert {i.compress_type for i in z.infolist()} == {zipfile.ZIP_STORED}
    assert z.read("originals/20260925-080100_cam_1.jpg") == jpeg(1)
    assert sorted(p.name for p in folder(tmp_path).iterdir()) == ["originals", "photos.csv"]
    assert api.get("/api/originals/zip", params={"profile_id": ann}).status_code == 404


def test_single_photo_download(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [
        ("A1.JPG", "2026-09-25 08:01:00", "cam"), ("A2.JPG", "2026-09-25 08:02:00", "cam"),
        ("A3.JPG", "2026-09-25 08:03:00", "cam")])
    g.add(1, "A1.JPG")
    g.add(2, "A2.JPG")
    g.images = {2: [httpx.Response(403)]}
    p = {rel: i for i, rel in conn.execute("select id, relpath from photos")}
    res = api.post(f"/api/photos/{p['1.jpg']}/original", json={"profile_id": ME})
    assert res.status_code == 200 and res.content == jpeg(1)
    assert res.headers["content-type"] == "image/jpeg" and "attachment" in res.headers["content-disposition"]
    assert files(tmp_path) == ["20260925-080100_cam_1.jpg"]
    before = len(g.lookups)
    assert api.post(f"/api/photos/{p['1.jpg']}/original", json={"profile_id": ME}).content == jpeg(1)
    assert len(g.lookups) == before
    res = api.post(f"/api/photos/{p['2.jpg']}/original", json={"profile_id": ME})
    assert res.status_code == 402 and res.json()["detail"] == "buy on site: HTTP 403"
    assert api.post(f"/api/photos/{p['3.jpg']}/original", json={"profile_id": ME}).status_code == 402
    assert api.post("/api/photos/999/original", json={"profile_id": ME}).status_code == 404
    times = [when for when, _, _ in g.lookups]
    assert len(times) == 3 and all(b - a >= 1.0 for a, b in zip(times, times[1:]))
    mine = api.get("/api/me", params={"profile_id": ME}).json()["photos"]
    assert [x["original"] for x in mine] == ["downloaded", "buy on site: HTTP 403", "buy on site: not found in the gallery"]


def test_cancel_signal_during_single_photo_is_409_not_500(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", "2026-09-25 08:01:00", "cam")])
    g.add(1, "A1.JPG")
    g.on_image = lambda pid: api.app.state.originals.fetcher.stop.set()
    photo = conn.execute("select id from photos").fetchone()[0]
    g.images = {1: [httpx.Response(503)]}
    res = api.post(f"/api/photos/{photo}/original", json={"profile_id": ME})
    assert res.status_code == 409 and "cancelled" in res.json()["detail"]
    assert files(tmp_path) == []
    g.on_image = lambda pid: None
    assert api.post(f"/api/photos/{photo}/original", json={"profile_id": ME}).content == jpeg(1)


def test_cancel_checks_and_sets_stop_atomically_with_job_state(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", None, "cam")])
    job = api.app.state.originals
    held = []

    class Stop(threading.Event):
        def set(self):
            held.append(job.lock.locked())
            super().set()
    job.fetcher.stop = Stop()
    job.cancel()
    assert held == []
    job.state["state"] = "running"
    job.cancel()
    assert held == [True]


def test_single_download_in_progress_blocks_rename_and_start(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", "2026-09-25 08:01:00", "cam")], mark=False)
    g.add(1, "A1.JPG")
    ann = api.post("/api/profiles", json={"name": "Ann"}).json()["id"]
    label(api, ids[1][0], "me", profile=ann)
    seen = []
    g.on_image = lambda pid: seen.extend([
        api.patch(f"/api/profiles/{ann}", json={"name": "Beth"}),
        api.post("/api/originals", json={"profile_id": ann})])
    photo = conn.execute("select id from photos").fetchone()[0]
    assert api.post(f"/api/photos/{photo}/original", json={"profile_id": ann}).status_code == 200
    assert [r.status_code for r in seen] == [409, 409]
    assert "in progress" in seen[0].json()["detail"] and "in progress" in seen[1].json()["detail"]
    assert [p["name"] for p in api.get("/api/profiles").json()["profiles"]] == ["Me", "Ann"]
    assert files(tmp_path, "Ann") == ["20260925-080100_cam_1.jpg"]
    assert api.get("/api/me", params={"profile_id": ann}).json()["photos"][0]["original"] == "downloaded"


def test_rename_commits_new_name_before_releasing_the_download_lock(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", None, "cam")], mark=False)
    ann = api.post("/api/profiles", json={"name": "Ann"}).json()["id"]
    (folder(tmp_path, "Ann") / "originals").mkdir(parents=True)
    job, seen = api.app.state.originals, []

    class Probe:
        lock = threading.Lock()

        def acquire(self, *a, **kw):
            return self.lock.acquire(*a, **kw)

        def release(self):
            other = sqlite3.connect(c / "index.sqlite")
            seen.append(other.execute("select name from profiles where id = ?", (ann,)).fetchone()[0])
            other.close()
            self.lock.release()
    job.busy = Probe()
    assert api.patch(f"/api/profiles/{ann}", json={"name": "Beth"}).status_code == 200
    assert api.patch(f"/api/profiles/{ann}", json={"name": "Cy"}).status_code == 200
    assert seen == ["Beth", "Cy"] and files(tmp_path, "Cy") == []


def test_start_resolves_profile_folder_after_claiming(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", "2026-09-25 08:01:00", "cam")], mark=False)
    g.add(1, "A1.JPG")
    ann = api.post("/api/profiles", json={"name": "Ann"}).json()["id"]
    label(api, ids[1][0], "me", profile=ann)
    (folder(tmp_path, "Ann") / "originals").mkdir(parents=True)
    rows_of, renames = originals.rows_of, []

    def rename_meanwhile(*a):
        if not renames:
            renames.append(api.patch(f"/api/profiles/{ann}", json={"name": "Beth"}).status_code)
        return rows_of(*a)
    monkeypatch.setattr(originals, "rows_of", rename_meanwhile)
    run(api, ann)
    name = api.get("/api/profiles").json()["profiles"][1]["name"]
    assert renames == [409] and name == "Ann"
    assert files(tmp_path, name) == ["20260925-080100_cam_1.jpg"]


def test_invalid_existing_file_is_removed_when_refetch_fails(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", "2026-09-25 08:01:00", "cam")], tries=2)
    g.add(1, "A1.JPG")
    g.images = {1: [httpx.Response(503), httpx.Response(503)]}
    d = folder(tmp_path) / "originals"
    d.mkdir(parents=True)
    (d / "20260925-080100_cam_1.jpg").write_bytes(jpeg(1)[:-2])
    status = run(api)
    assert status["counts"]["failed"] == 1 and files(tmp_path) == []
    assert [r[7:] for r in read_csv(tmp_path)[1:]] == [["", "failed: HTTP 503"]]
    assert api.get("/api/me", params={"profile_id": ME}).json()["photos"][0]["original"] == "failed: HTTP 503"
    assert api.get("/api/originals/zip", params={"profile_id": ME}).status_code == 404


def test_csv_without_group_column_keeps_its_statuses(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", "2026-09-25 08:01:00", "cam")])
    old = ["source_photo_id", "original_file_name", "photographer", "taken_at", "album", "preview_path",
           "original_path", "status"]
    folder(tmp_path).mkdir(parents=True)
    (folder(tmp_path) / "photos.csv").write_text(
        ",".join(old) + "\n1,A1.JPG,cam,,9.25 赛事,x,,buy on site: HTTP 403\n", encoding="utf-8-sig")
    assert api.post("/api/export", json={"profile_id": ME}).status_code == 200
    rows = read_csv(tmp_path)
    assert rows[0] == originals.COLUMNS and rows[1][4:6] == ["", "9.25 赛事"]
    assert rows[1][8] == "buy on site: HTTP 403"


def test_rename_after_download_rewrites_csv_paths(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", "2026-09-25 08:01:00", "cam")], mark=False)
    g.add(1, "A1.JPG")
    ann = api.post("/api/profiles", json={"name": "Ann B"}).json()["id"]
    label(api, ids[1][0], "me", profile=ann)
    run(api, ann)
    assert api.patch(f"/api/profiles/{ann}", json={"name": "Bob"}).status_code == 200
    original = folder(tmp_path, "Bob") / "originals" / "20260925-080100_cam_1.jpg"
    assert [(r[7], r[8]) for r in read_csv(tmp_path, "Bob")[1:]] == [(str(original), "downloaded")]
    z = zipfile.ZipFile(io.BytesIO(api.get("/api/originals/zip", params={"profile_id": ann}).content))
    assert str(original) in z.read("photos.csv").decode("utf-8-sig")


def test_profile_folders_are_traversal_safe(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", "2026-09-25 08:01:00", "cam")], mark=False)
    g.add(1, "A1.JPG")
    exports = tmp_path / "data" / "exports" / "coll"
    for name, safe in (("../x", "x"), ("/etc", "etc"), ("..", "profile"), (".hidden", "hidden")):
        assert originals.profile_folder(c, name) == exports / safe
        pid = api.post("/api/profiles", json={"name": name}).json()["id"]
        label(api, ids[1][0], "me", profile=pid)
        run(api, pid)
        assert files(tmp_path, safe) == ["20260925-080100_cam_1.jpg"]
    written = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()} - \
        {p.relative_to(tmp_path) for p in c.rglob("*")}
    assert all(p.parts[:3] == ("data", "exports", "coll") for p in written), written


def test_folder_collisions_rejected_and_rename_moves_folder(tmp_path, monkeypatch):
    c, conn, ids, t, g, api = setup(tmp_path, monkeypatch, [("A1.JPG", "2026-09-25 08:01:00", "cam")], mark=False)
    for name in ("me", "ME", "Me!"):
        res = api.post("/api/profiles", json={"name": name})
        assert res.status_code == 400 and "folder" in res.json()["detail"], name
    ann = api.post("/api/profiles", json={"name": "Ann B"}).json()["id"]
    assert api.post("/api/profiles", json={"name": "ann_b"}).status_code == 400
    assert api.patch(f"/api/profiles/{ME}", json={"name": "Ann/B"}).status_code == 400
    assert api.patch(f"/api/profiles/{ann}", json={"name": "Ann B"}).status_code == 200
    (folder(tmp_path, "Ann_B") / "originals").mkdir(parents=True)
    (folder(tmp_path, "Ann_B") / "originals" / "x.jpg").write_bytes(jpeg(1))
    assert api.patch(f"/api/profiles/{ann}", json={"name": "Bob"}).status_code == 200
    assert files(tmp_path, "Bob") == ["x.jpg"] and not folder(tmp_path, "Ann_B").exists()
    assert api.patch(f"/api/profiles/{ann}", json={"name": "bob"}).status_code == 200
    assert [p.name for p in folder(tmp_path).parent.iterdir()] == ["bob"]
    (folder(tmp_path, "Cy")).mkdir()
    res = api.patch(f"/api/profiles/{ann}", json={"name": "Cy"})
    assert res.status_code == 400 and "already exists" in res.json()["detail"]
    assert [p["name"] for p in api.get("/api/profiles").json()["profiles"]] == ["Me", "bob"]
    assert files(tmp_path, "bob") == ["x.jpg"]


def test_non_yipai_collection_has_no_originals(tmp_path, monkeypatch):
    c, conn, ids = make_index(tmp_path, [(1, (0, 0, 50, 100), A, X)], photos=1)
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data")
    api = RaceClient(web.create_app(c))
    label(api, ids[1][0], "me")
    assert api.get("/api/facets").json()["originals"] is False
    photo = conn.execute("select id from photos").fetchone()[0]
    for res in (api.post("/api/originals", json={"profile_id": ME}),
                api.get("/api/originals/zip", params={"profile_id": ME}),
                api.post(f"/api/photos/{photo}/original", json={"profile_id": ME})):
        assert res.status_code == 400 and "yipai360" in res.json()["detail"], res.text
    rows = list(csv.reader(open(api.post("/api/export", json={"profile_id": ME}).json()["path"],
                                encoding="utf-8-sig")))
    assert rows[1] == ["1", "", "", "", "", "", str(c.resolve() / "1.jpg"), "", ""]
    (tmp_path / "y").mkdir()
    c2, _, _ = yipai_index(tmp_path / "y", monkeypatch, [("A1.JPG", None, None)])
    assert RaceClient(web.create_app(c2)).get("/api/facets").json()["originals"] is True


@pytest.mark.parametrize("name,safe", [("Ann B", "Ann_B"), ("../../etc", "etc"), ("阿光", "阿光"), ("", "profile"),
                                       ("é", "é")])
def test_safe_name(name, safe):
    assert originals.safe_name(name) == safe
