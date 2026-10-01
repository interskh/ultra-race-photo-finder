import csv
import io
import sqlite3
import threading
import time
import zipfile

import httpx
import pytest

from photofinder import config, originals
from photofinder.sources import base, photoplus
from photofinder.web import app as web
from test_originals import jpeg
from test_search import A, X, make_index
from test_web import RaceClient, ME, label, no_real_models  # noqa: F401
from test_yipai import FakeTime

ACT = "777"
PAGE = 3
PP_FOLDER = "photoplus-" + ACT


def pos_id(p):
    return 200 - p


def when(p):
    return f"2026-09-25 09:{60 - {7: 6, 10: 9}.get(p, p):02d}:00"


ENTRIES = [(pos_id(p), when(p)) for p in range(1, 14)]


class PP:
    def __init__(self, t, entries=ENTRIES):
        self.t, self.entries = t, entries
        self.listings, self.fetched = [], []
        self.images = {}
        self.api = lambda req: None
        self.on_image = lambda pid: None
        self.body = jpeg
        self.declare = False

    def __call__(self, req):
        if req.url.path == "/pic/list":
            q = req.url.params
            assert q["activityNo"] == ACT and q["_s"] and q["isNew"] == "false"
            n, c = int(q["page"]), int(q["count"])
            self.listings.append((self.t.now, n))
            forced = self.api(req)
            if forced is not None:
                return forced
            return httpx.Response(200, json={"code": 1, "result": {"pics_total": len(self.entries), "pics_array": [
                {"id": i, "relate_time": tm, "watermark_origin_img": f"//img.test/o/{i}{self.size(i)}.JPG?sign=1"}
                for i, tm in self.entries[(n - 1) * c: n * c]]}})
        assert req.url.host == "img.test" and req.headers["referer"] == photoplus.API + "/"
        pid = int(req.url.path.rsplit("/", 1)[1][:-4].split(":")[0])
        self.fetched.append(pid)
        self.on_image(pid)
        replies = self.images.get(pid)
        return replies.pop(0) if replies else httpx.Response(200, content=self.body(pid))

    def size(self, pid):
        return f":{len(self.body(pid))}" if self.declare else ""

    def pages(self):
        return [n for _, n in self.listings]


def pp_index(tmp_path, monkeypatch, marks, others=()):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(photoplus, "LIST_PAGE", PAGE)
    n = len(marks) + len(others)
    c, conn, ids = make_index(tmp_path, [(i, (0, 0, 50, 100), A, X) for i in range(1, n + 1)], photos=n)
    entries = [(PP_FOLDER, str(sid), shot) for sid, shot in marks] + [(a, str(sid), shot) for a, sid, shot in others]
    for i, (album, sid, shot) in enumerate(entries, 1):
        conn.execute("update photos set source_photo_id = ?, album_key = ?, photographer = 'cam', taken_at = ? "
                     "where relpath = ?", (sid, album, shot, f"{i}.jpg"))
    conn.commit()
    for album in dict.fromkeys(e[0] for e in entries):
        folder = c / "albums" / album
        folder.mkdir(parents=True)
        m = sqlite3.connect(folder / "manifest.sqlite")
        m.executescript(base.SCHEMA)
        m.executemany("insert into catalog(source_id, fname, taken_at) values (?,?,?)",
                      [(sid, f"IMG_{sid}.JPG", shot) for a, sid, shot in entries if a == album])
        m.commit()
        m.close()
    return c, conn, ids


def setup(tmp_path, monkeypatch, marks, others=(), **kw):
    c, conn, ids = pp_index(tmp_path, monkeypatch, marks, others)
    t = FakeTime()
    g = PP(t)
    fetcher = originals.Fetcher(httpx.Client(transport=httpx.MockTransport(g)), sleep=t.sleep, clock=t.clock, **kw)
    api = RaceClient(web.create_app(c, fetcher=fetcher))
    for pids in ids.values():
        label(api, pids[0], "me")
    return c, conn, t, g, api


def run(api):
    assert api.post("/api/originals", json={"profile_id": ME}).status_code == 200
    api.app.state.originals.thread.join(10)
    status = api.get("/api/originals").json()
    assert status["state"] != "running"
    return status


def pos(p):
    return pos_id(p), when(p)


def out_dir(tmp_path):
    return tmp_path / "data" / "exports" / "coll" / "Me" / "originals"


def names(tmp_path):
    d = out_dir(tmp_path)
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_first_page_hit_costs_two_listings_and_downloads_the_full_size_file(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(2)])
    status = run(api)
    assert status["state"] == "done" and status["counts"]["downloaded"] == 1
    assert g.pages() == [1, 3] and g.fetched == [198]
    assert names(tmp_path) == [f"20260925-095800_cam_photoplus-198.jpg"]
    assert (out_dir(tmp_path) / names(tmp_path)[0]).read_bytes() == jpeg(198)


def test_bisection_reaches_a_middle_and_a_last_page(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(11), pos(13), pos(5)])
    status = run(api)
    assert status["counts"]["downloaded"] == 3 and sorted(g.fetched) == [187, 189, 195]
    assert g.pages() == [1, 3, 4, 5, 2]
    assert all(b - a >= photoplus.PAGE_GAP for (a, _), (b, _) in zip(g.listings, g.listings[1:]))


@pytest.mark.parametrize("p, expected", [(6, [1, 3, 2]), (7, [1, 3]), (9, [1, 3]), (10, [1, 3, 4])])
def test_tie_across_a_page_boundary_scans_the_adjacent_page(tmp_path, monkeypatch, p, expected):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(p)])
    status = run(api)
    assert status["counts"]["downloaded"] == 1 and g.fetched == [pos_id(p)]
    assert g.pages() == expected


def test_absent_id_and_missing_shot_time_fail_without_stopping_the_job(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [(999, when(4)), (pos_id(9), None), (pos_id(8), when(8)),
                                                       (pos_id(5), "2026-09-25 03:00:00")])
    status = run(api)
    assert status["state"] == "done" and status["done"] == 4
    assert status["counts"] == {"downloaded": 1, "skipped": 0, "buy_on_site": 0, "failed": 3, "open_on_site": 0}
    assert sorted(e.split(": ", 1)[1] for e in status["errors"]) == [
        "failed: no shot time recorded", "failed: not found in the photoplus album",
        "failed: not found in the photoplus album"]
    assert g.fetched == [pos_id(8)]


def test_expired_link_relists_the_page_once_then_downloads(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(2)])
    g.images = {198: [httpx.Response(403)]}
    status = run(api)
    assert status["counts"]["downloaded"] == 1 and g.fetched == [198, 198]
    assert g.pages() == [1, 3, 1]


def test_second_403_is_a_failure_not_a_purchase_prompt(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(2), pos(4)])
    g.images = {198: [httpx.Response(403), httpx.Response(403)]}
    status = run(api)
    assert status["counts"] == {"downloaded": 1, "skipped": 0, "buy_on_site": 0, "failed": 1, "open_on_site": 0}
    assert status["errors"] == ["198 IMG_198.JPG: failed: photoplus refused the download link again after relisting "
                                "(HTTP 403)"]
    assert sorted(g.fetched) == [196, 198, 198]
    assert names(tmp_path) == ["20260925-095600_cam_photoplus-196.jpg"]


def test_non_jpeg_original_is_buy_on_site(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(2)])
    g.images = {198: [httpx.Response(200, content=b"<html>pay</html>" * 50)]}
    assert run(api)["errors"] == ["198 IMG_198.JPG: buy on site: not a JPEG"]


def test_api_blocked_names_photoplus_for_the_job_and_the_single_download(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(2), pos(4)])
    g.api = lambda req: httpx.Response(401)
    status = run(api)
    assert status["state"] == "error" and status["done"] == 0
    assert status["errors"][-1].startswith("photoplus API unavailable") and g.fetched == []
    photo = conn.execute("select id from photos where relpath = '1.jpg'").fetchone()[0]
    res = api.post(f"/api/photos/{photo}/original", json={"profile_id": ME})
    assert res.status_code == 502 and res.json()["detail"].startswith("photoplus API unavailable")


def test_cancel_interrupts_the_listing_gap(tmp_path, monkeypatch):
    c, conn, ids = pp_index(tmp_path, monkeypatch, [pos(11)])
    g = PP(FakeTime())
    looked = threading.Event()
    g.api = lambda req: looked.set()
    fetcher = originals.Fetcher(httpx.Client(transport=httpx.MockTransport(g)))
    fetcher.locator.gap = 60
    api = RaceClient(web.create_app(c, fetcher=fetcher))
    label(api, ids[1][0], "me")
    assert api.post("/api/originals", json={"profile_id": ME}).status_code == 200
    assert looked.wait(5)
    t0 = time.monotonic()
    api.post("/api/originals/cancel")
    api.app.state.originals.thread.join(5)
    assert time.monotonic() - t0 < 5 and api.get("/api/originals").json()["state"] == "cancelled"
    assert g.pages() == [1] and g.fetched == []


def test_pages_are_cached_between_nearby_photos_and_expire(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(2), pos(3), pos(5)])
    run(api)
    assert g.pages() == [1, 3, 2]
    loc = api.app.state.originals.fetcher.locator
    g.listings.clear()
    t.now += photoplus.PAGE_TTL + 1
    loc.locate(ACT, when(2), str(pos_id(2)))
    assert g.pages() == [1, 3]


def test_listing_budget_bounds_a_shifting_album(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(11)])
    api.app.state.originals.fetcher.locator.budget = 2
    status = run(api)
    assert status["counts"]["failed"] == 1 and len(g.listings) == 2
    assert "request limit reached" in status["errors"][0]


def test_rerun_skips_valid_files_without_requests(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(2), pos(8)])
    run(api)
    g.listings.clear(), g.fetched.clear()
    status = run(api)
    assert status["counts"]["skipped"] == 2 and (g.listings, g.fetched) == ([], [])


def test_mixed_race_photoplus_downloads_pailixiang_stays_open_on_site_csv_zip_and_viewer(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(2)], others=[("pailixiang-zz", "198", when(2))])
    assert api.get("/api/facets").json()["originals"] is True
    status = run(api)
    assert status["counts"] == {"downloaded": 1, "skipped": 0, "buy_on_site": 0, "failed": 0, "open_on_site": 1}
    assert g.fetched == [198]
    rows = list(csv.DictReader(io.StringIO((out_dir(tmp_path).parent / "photos.csv").read_text("utf-8-sig"))))
    by = {r["status"]: r for r in rows}
    assert by["downloaded"]["original_path"] == str(out_dir(tmp_path) / names(tmp_path)[0])
    assert by["open on site"]["original_path"] == ""
    z = zipfile.ZipFile(io.BytesIO(api.get("/api/originals/zip", params={"profile_id": ME}).content))
    assert sorted(z.namelist()) == [f"originals/{names(tmp_path)[0]}", "photos.csv"]
    one, other = (conn.execute("select id from photos where relpath = ?", (r,)).fetchone()[0]
                  for r in ("1.jpg", "2.jpg"))
    (out_dir(tmp_path) / names(tmp_path)[0]).unlink()
    res = api.post(f"/api/photos/{one}/original", json={"profile_id": ME})
    assert res.status_code == 200 and res.content == jpeg(198) and res.headers["content-type"] == "image/jpeg"
    res = api.post(f"/api/photos/{other}/original", json={"profile_id": ME})
    assert res.status_code == 409 and "yipai360 and photoplus" in res.json()["detail"]


def test_downloadable_and_file_names_per_platform():
    assert [originals.downloadable({"platform": p}) for p in (None, "yipai", "photoplus", "pailixiang", "xxpie")] == \
        [True, True, True, False, False]
    meta = {"taken_at": "2026-09-25 08:00:00", "photographer": "cam", "source_photo_id": "101"}
    assert originals.file_name(meta) == originals.file_name(meta, "yipai") == "20260925-080000_cam_101.jpg"
    assert originals.file_name(meta, "photoplus") == "20260925-080000_cam_photoplus-101.jpg"


def locator_for(entries, gap=photoplus.PAGE_GAP, **kw):
    t = FakeTime()
    g = PP(t, entries)
    return photoplus.Locator(httpx.Client(transport=httpx.MockTransport(g)), pause=t.sleep, clock=t.clock, gap=gap,
                             **kw), g


def album(n):
    return [(1000 + n - i, f"2026-09-25 09:{59 - i:02d}:00") for i in range(n)]


@pytest.mark.parametrize("cached, target", [(3, 1007), (1, 1001)])
def test_new_upload_between_cached_and_fresh_pages_does_not_lose_the_photo(monkeypatch, cached, target):
    monkeypatch.setattr(photoplus, "LIST_PAGE", PAGE)
    entries = album(12)
    loc, g = locator_for(entries)
    loc.page(ACT, cached, [0, 0])
    shot = dict((i, tm) for i, tm in entries)[target]
    entries.insert(0, (1013, "2026-09-25 10:00:00"))
    assert loc.locate(ACT, shot, str(target)).endswith(f"/o/{target}.JPG?sign=1")


def test_genuinely_absent_photo_is_retried_fresh_once_then_not_found(monkeypatch):
    monkeypatch.setattr(photoplus, "LIST_PAGE", PAGE)
    loc, g = locator_for(album(12))
    first = album(12)[1]
    loc.locate(ACT, first[1], str(first[0]))
    g.listings.clear()
    with pytest.raises(photoplus.NotFound, match="not found"):
        loc.locate(ACT, first[1], "999")
    assert g.pages().count(1) == 1 and len(g.listings) <= 4


def test_missing_pics_total_is_an_error_not_a_silent_miss(monkeypatch):
    monkeypatch.setattr(photoplus, "LIST_PAGE", PAGE)
    loc, g = locator_for(album(12))
    g.api = lambda req: httpx.Response(200, json={"code": 1, "result": {"pics_array": [
        {"id": 1, "relate_time": when(1)}]}})
    with pytest.raises(photoplus.NotFound, match="did not report the album size"):
        loc.locate(ACT, when(1), "1")


def shaped(pid):
    return b"\xff\xd8" + f"photo {pid}".encode() + b"x" * 3000 + b"\xff\xd9" + b"t" * 500


def test_real_shaped_original_with_trailing_data_downloads_and_reruns_without_requests(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(2)])
    g.body, g.declare = shaped, True
    status = run(api)
    assert status["counts"]["downloaded"] == 1 and g.fetched == [198]
    assert (out_dir(tmp_path) / names(tmp_path)[0]).read_bytes() == shaped(198)
    g.listings.clear(), g.fetched.clear()
    status = run(api)
    assert status["counts"]["skipped"] == 1 and (g.listings, g.fetched) == ([], [])
    photo = conn.execute("select id from photos").fetchone()[0]
    assert api.post(f"/api/photos/{photo}/original", json={"profile_id": ME}).content == shaped(198)
    assert (g.listings, g.fetched) == ([], [])


def test_size_mismatch_against_the_declared_length_is_retried_then_fails(tmp_path, monkeypatch):
    c, conn, t, g, api = setup(tmp_path, monkeypatch, [pos(2)], tries=3)
    g.declare = True
    g.images = {198: [httpx.Response(200, content=shaped(198)[:-100])] * 2}
    status = run(api)
    assert status["counts"]["downloaded"] == 1 and g.fetched == [198] * 3
    g.listings.clear(), g.fetched.clear()
    (out_dir(tmp_path) / names(tmp_path)[0]).unlink()
    g.images = {198: [httpx.Response(200, content=shaped(198)[:-100])] * 3}
    status = run(api)
    assert status["counts"]["failed"] == 1 and status["errors"] == ["198 IMG_198.JPG: failed: truncated download"]
    assert names(tmp_path) == []


def test_budget_counts_every_http_attempt_not_just_pages(monkeypatch):
    monkeypatch.setattr(photoplus, "LIST_PAGE", PAGE)
    loc, g = locator_for(album(12), budget=3)
    g.api = lambda req: httpx.Response(500)
    with pytest.raises(photoplus.BudgetExceeded):
        loc.locate(ACT, when(2), "1")
    assert len(g.listings) == 3


def test_retry_sleeps_are_never_shorter_than_the_listing_gap(monkeypatch):
    monkeypatch.setattr(photoplus, "LIST_PAGE", PAGE)
    monkeypatch.setattr("photofinder.sources.common.backoff_seconds", lambda attempt: 1.0)
    loc, g = locator_for(album(12), budget=4)
    g.api = lambda req: httpx.Response(500)
    with pytest.raises(photoplus.BudgetExceeded):
        loc.locate(ACT, when(2), "1")
    times = [when_ for when_, _ in g.listings]
    assert len(times) == 4 and all(b - a >= photoplus.PAGE_GAP for a, b in zip(times, times[1:]))


def test_fresh_retry_gets_its_own_budget(monkeypatch):
    monkeypatch.setattr(photoplus, "LIST_PAGE", PAGE)
    entries = album(12)
    loc, g = locator_for(entries, budget=2)
    loc.page(ACT, 3, [0, 0])
    shot = dict(entries)[1007]
    entries.insert(0, (1013, "2026-09-25 10:00:00"))
    assert loc.locate(ACT, shot, "1007").endswith("/o/1007.JPG?sign=1")
