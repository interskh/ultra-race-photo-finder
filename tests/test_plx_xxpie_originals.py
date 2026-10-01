import csv
import io
import json
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from photofinder import config, originals
from photofinder.sources import base, pailixiang, xxpie, yipai
from photofinder.web import app as web
from test_originals import ORDER, Gallery, jpeg
from test_search import A, X, make_index
from test_web import RaceClient, ME, label, no_real_models  # noqa: F401
from test_yipai import FakeTime

PLX = "pailixiang-a1"
XXP = "xxpie-abc"
YP = "yipai-" + ORDER


def shaped(pid):
    return b"\xff\xd8" + f"photo {pid}".encode() + b"x" * 3000 + b"\xff\xd9" + b"t" * 500


class World:
    def __init__(self, t):
        self.t = t
        self.gallery = Gallery(t)
        self.plx, self.xx, self.images, self.registrations = [], [], [], []
        self.items, self.photos, self.replies, self.forced = [], [], {}, {}
        self.body = jpeg
        self.size_off = {}
        self.bad_codes = 0

    def add_plx(self, sid, name):
        self.items.append({"ID": str(sid), "Name": name})

    def add_xx(self, sid, name):
        self.photos.append({"album_ossobject_id": str(sid), "file_name": name})

    def plx_item(self, p):
        size = len(self.body(p["ID"])) + self.size_off.get(p["ID"], 0)
        return {**p, "FileSize1": size, "DownloadImageUrl": f"https://oss.test/plx/{p['ID']}?Expires=1"}

    def __call__(self, req):
        host = req.url.host
        if host == "mapi.pailixiang.com":
            action = req.url.path.rsplit("/", 1)[1]
            body = json.loads(req.content)
            self.plx.append((self.t.now, action, body))
            if forced := self.forced.get("plx"):
                return forced(req)
            if action == "AlbumGetView":
                assert body["ID"] == "1"
                return httpx.Response(200, json={"Code": 0, "Data": {"Entity": {"ID": "INT1", "Title": "t"}}})
            assert action == "AlbumSearchPhoto" and body["AlbumID"] == "INT1"
            hits = [self.plx_item(p) for p in self.items if body["SearchText"] in (p["Name"], p["Name"].split(".")[0])]
            start, count = body["StartIndex"], body["SearchCount"]
            return httpx.Response(200, json={"Code": 0, "TotalCount": 999, "Data": hits[start - 1: start - 1 + count]})
        if host == "int.xxpie.com":
            if req.url.path.endswith("registerVisitorUser"):
                self.registrations.append(self.t.now)
                if cb := self.forced.get("reg"):
                    cb(req)
                return httpx.Response(200, json={"code": 0, "result": {"token": f"tok{len(self.registrations)}"}})
            q = req.url.params
            self.xx.append((self.t.now, dict(q), req.headers.get("referer")))
            if forced := self.forced.get("xx"):
                return forced(req)
            if self.bad_codes:
                self.bad_codes -= 1
                return httpx.Response(200, json={"code": 401, "message": "token expired"})
            assert req.headers["x-access-token"] == f"tok{len(self.registrations)}"
            assert req.url.path == "/api/pm/queryAlbumItemsPgByDefaultSort"
            hits = [dict(p, url_origin=f"https://imagex.test/o/{p['album_ossobject_id']}:"
                         f"{len(self.body(p['album_ossobject_id'])) + self.size_off.get(p['album_ossobject_id'], 0)}"
                         f".jpeg?sign=1")
                    for p in self.photos if q["file_name"] in (p["file_name"], p["file_name"].split(".")[0])]
            n, size = int(q["page_no"]), int(q["page_size"])
            return httpx.Response(200, json={"code": 0, "result": {"photos": hits[(n - 1) * size: n * size]}})
        if host in ("oss.test", "imagex.test"):
            pid = req.url.path.rsplit("/", 1)[1].split(":")[0]
            self.images.append((host, pid, dict(req.headers)))
            if host == "oss.test" and "content-type" in req.headers:
                return httpx.Response(403, text="SignatureDoesNotMatch")
            replies = self.replies.get(pid)
            return replies.pop(0) if replies else httpx.Response(200, content=self.body(pid))
        return self.gallery(req)

    def ids(self, host=None):
        return [pid for h, pid, _ in self.images if host in (None, h)]


def build(tmp_path, monkeypatch, entries):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(pailixiang, "PAGE", 2)
    monkeypatch.setattr(xxpie, "PAGE", 2)
    n = len(entries)
    c, conn, ids = make_index(tmp_path, [(i, (0, 0, 50, 100), A, X) for i in range(1, n + 1)], photos=n)
    for i, (album, sid, fname, shot) in enumerate(entries, 1):
        conn.execute("update photos set source_photo_id = ?, album_key = ?, photographer = 'cam', taken_at = ? "
                     "where relpath = ?", (sid, album, shot, f"{i}.jpg"))
    conn.commit()
    for album in dict.fromkeys(e[0] for e in entries):
        folder = c / "albums" / album
        folder.mkdir(parents=True)
        m = sqlite3.connect(folder / "manifest.sqlite")
        if album.startswith("yipai"):
            m.executescript(yipai.SCHEMA)
            m.executemany("insert into photos(photo_id, order_id, fname) values (?,?,?)",
                          [(int(sid), ORDER, fname) for a, sid, fname, _ in entries if a == album])
        else:
            m.executescript(base.SCHEMA)
            m.executemany("insert into catalog(source_id, fname, taken_at) values (?,?,?)",
                          [(sid, fname, shot) for a, sid, fname, shot in entries if a == album])
        m.commit()
        m.close()
    return c, conn, ids


SHOT = "2026-09-25 09:30:00"


def setup(tmp_path, monkeypatch, entries, **kw):
    c, conn, ids = build(tmp_path, monkeypatch, entries)
    t = FakeTime()
    w = World(t)
    for album, sid, fname, _ in entries:
        if album == PLX:
            w.add_plx(sid, fname)
        elif album == XXP:
            w.add_xx(sid, fname)
        else:
            w.gallery.add(int(sid), fname)
    fetcher = originals.Fetcher(httpx.Client(transport=httpx.MockTransport(w)), sleep=t.sleep, clock=t.clock, **kw)
    api = RaceClient(web.create_app(c, fetcher=fetcher))
    for pids in ids.values():
        label(api, pids[0], "me")
    return c, conn, t, w, api


def run(api):
    assert api.post("/api/originals", json={"profile_id": ME}).status_code == 200
    api.app.state.originals.thread.join(10)
    status = api.get("/api/originals").json()
    assert status["state"] != "running"
    return status


def out_dir(tmp_path):
    return tmp_path / "data" / "exports" / "coll" / "Me" / "originals"


def names(tmp_path):
    d = out_dir(tmp_path)
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def plx(sid, name=None):
    return PLX, str(sid), name or f"DSC_{sid}.JPG", SHOT


def xxp(sid, name=None):
    return XXP, str(sid), name or f"IMG_{sid}.JPG", SHOT


def searches(w):
    return [b for _, a, b in w.plx if a == "AlbumSearchPhoto"]


def test_pailixiang_lookup_downloads_with_a_platform_file_name_and_no_content_type(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(555)])
    status = run(api)
    assert status["state"] == "done" and status["counts"]["downloaded"] == 1
    assert [a for _, a, _ in w.plx] == ["AlbumGetView", "AlbumSearchPhoto"]
    assert searches(w)[0]["SearchText"] == "DSC_555.JPG"
    assert names(tmp_path) == ["20260925-093000_cam_pailixiang-555.jpg"]
    assert (out_dir(tmp_path) / names(tmp_path)[0]).read_bytes() == jpeg("555")
    headers = w.images[0][2]
    assert "content-type" not in headers and headers["referer"] == pailixiang.SITE + "/"


def test_pailixiang_duplicate_names_pick_the_matching_id_and_page_on_when_a_page_is_full(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(502, "DSC_1.JPG")])
    for sid in (500, 501):
        w.add_plx(sid, "DSC_1.JPG")
    w.items.sort(key=lambda p: p["ID"])
    status = run(api)
    assert status["counts"]["downloaded"] == 1 and w.ids() == ["502"]
    assert [b["StartIndex"] for b in searches(w)] == [1, 3]


def test_pailixiang_not_found_fails_the_row_and_the_job_continues(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(1), plx(2)])
    w.items = [p for p in w.items if p["ID"] != "1"]
    status = run(api)
    assert status["state"] == "done" and status["counts"]["failed"] == 1 and status["counts"]["downloaded"] == 1
    assert status["errors"] == ["1 DSC_1.JPG: failed: not found in the pailixiang album"]
    assert w.ids() == ["2"]


def test_pailixiang_full_pages_without_a_match_stop_after_the_bound(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(9, "DSC_1.JPG")])
    w.items = [{"ID": str(100 + i), "Name": "DSC_1.JPG"} for i in range(40)]
    status = run(api)
    assert status["errors"] == ["9 DSC_1.JPG: failed: not found in the pailixiang album"]
    assert len(searches(w)) == pailixiang.MAX_LOOKUP_PAGES


def test_requests_per_platform_keep_the_lookup_gap(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(1), plx(2), xxp(3), xxp(4)])
    run(api)
    for times in ([n for n, _, _ in w.plx], [n for n, _, _ in w.xx]):
        assert len(times) >= 2 and all(b - a >= pailixiang.LOOKUP_GAP - 1e-6 for a, b in zip(times, times[1:]))


def test_pailixiang_size_mismatch_is_retried_then_fails_and_a_matching_size_passes(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(7)], tries=3)
    w.size_off["7"] = 5
    status = run(api)
    assert status["errors"] == ["7 DSC_7.JPG: failed: truncated download"] and w.ids() == ["7"] * 3
    assert names(tmp_path) == []
    w.size_off.clear()
    assert run(api)["counts"]["downloaded"] == 1


def test_pailixiang_body_without_eoi_is_truncated_even_when_the_size_matches(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(7)], tries=2)
    w.body = lambda pid: b"\xff\xd8" + b"x" * 3000
    assert run(api)["errors"] == ["7 DSC_7.JPG: failed: truncated download"]


def test_pailixiang_expired_link_relooks_up_once_then_fails_on_a_second_403(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(7), plx(8)])
    w.replies = {"7": [httpx.Response(403)], "8": [httpx.Response(403), httpx.Response(403)]}
    status = run(api)
    assert status["counts"]["downloaded"] == 1 and w.ids() == ["7", "7", "8", "8"]
    assert [b["SearchText"] for b in searches(w)] == ["DSC_7.JPG"] * 2 + ["DSC_8.JPG"] * 2
    assert status["errors"] == ["8 DSC_8.JPG: failed: pailixiang refused the download link again after relisting "
                                "(HTTP 403)"]


def test_xxpie_lookup_params_declared_size_and_trailing_data(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [xxp(77)])
    w.body = shaped
    status = run(api)
    assert status["counts"]["downloaded"] == 1 and names(tmp_path) == ["20260925-093000_cam_xxpie-77.jpg"]
    assert (out_dir(tmp_path) / names(tmp_path)[0]).read_bytes() == shaped("77")
    _, q, _ = w.xx[0]
    assert q == {"album_id": "abc", "page_no": "1", "page_size": "2", "file_name": "IMG_77.JPG", "platform": "H5"}
    headers = w.images[0][2]
    assert headers["referer"] == xxpie.SITE + "/"


def test_xxpie_size_mismatch_is_retried_then_fails(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [xxp(77)], tries=3)
    w.body = shaped
    w.size_off["77"] = 100
    status = run(api)
    assert status["errors"] == ["77 IMG_77.JPG: failed: truncated download"] and w.ids() == ["77"] * 3
    assert names(tmp_path) == []


def test_xxpie_duplicates_paging_and_not_found(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [xxp(53, "IMG_1.JPG"), xxp(54, "IMG_2.JPG")])
    w.photos = [{"album_ossobject_id": str(i), "file_name": "IMG_1.JPG"} for i in (50, 51, 52, 53)]
    status = run(api)
    assert w.ids() == ["53"] and [q["page_no"] for _, q, _ in w.xx[:2]] == ["1", "2"]
    assert status["errors"] == ["54 IMG_2.JPG: failed: not found in the xxpie album"]


def test_xxpie_expired_link_relooks_up_once_then_fails_on_a_second_403(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [xxp(7), xxp(8)])
    w.replies = {"7": [httpx.Response(403)], "8": [httpx.Response(403), httpx.Response(403)]}
    status = run(api)
    assert status["counts"]["downloaded"] == 1 and w.ids() == ["7", "7", "8", "8"] and len(w.xx) == 4
    assert status["errors"] == ["8 IMG_8.JPG: failed: xxpie refused the download link again after relisting "
                                "(HTTP 403)"]


def test_xxpie_token_is_renewed_when_a_lookup_answers_a_non_zero_code(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [xxp(7)])
    w.bad_codes = 1
    status = run(api)
    assert status["counts"]["downloaded"] == 1 and len(w.registrations) == 2 and len(w.xx) == 2
    assert w.xx[1][0] - w.xx[0][0] >= xxpie.LOOKUP_GAP


@pytest.mark.parametrize("entry, kind", [(plx(1), "plx"), (xxp(2), "xx")])
def test_cancel_interrupts_the_lookup_gap(tmp_path, monkeypatch, entry, kind):
    c, conn, ids = build(tmp_path, monkeypatch, [entry, plx(3) if kind == "plx" else xxp(4)])
    w = World(FakeTime())
    for album, sid, fname, _ in (entry, plx(3) if kind == "plx" else xxp(4)):
        (w.add_plx if album == PLX else w.add_xx)(sid, fname)
    looked = threading.Event()
    ok = {"Code": 0, "Data": {"Entity": {"ID": "INT1"}}} if kind == "plx" else {"code": 0, "result": {}}

    def forced(req):
        looked.set()
        return httpx.Response(200, json=ok)
    w.forced[kind] = forced
    w.forced["reg"] = lambda req: looked.set()
    fetcher = originals.Fetcher(httpx.Client(transport=httpx.MockTransport(w)))
    fetcher.plx_locator.gap = fetcher.xxpie_locator.gap = 60
    api = RaceClient(web.create_app(c, fetcher=fetcher))
    for pids in ids.values():
        label(api, pids[0], "me")
    assert api.post("/api/originals", json={"profile_id": ME}).status_code == 200
    assert looked.wait(5)
    t0 = time.monotonic()
    api.post("/api/originals/cancel")
    api.app.state.originals.thread.join(5)
    assert time.monotonic() - t0 < 5 and api.get("/api/originals").json()["state"] == "cancelled"
    assert w.ids() == []


def test_rerun_skips_existing_files_with_no_requests(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(1), xxp(2)])
    assert run(api)["counts"]["downloaded"] == 2
    before = (len(w.plx), len(w.xx), len(w.images))
    status = run(api)
    assert status["counts"]["skipped"] == 2 and (len(w.plx), len(w.xx), len(w.images)) == before


def test_outage_of_one_platform_fails_its_rows_without_requests_and_other_platforms_finish(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(1), (YP, "101", "IMG_101.JPG", SHOT), plx(2), xxp(3)])
    w.forced["plx"] = lambda req: httpx.Response(401)
    status = run(api)
    assert status["state"] == "error" and status["done"] == 4
    assert status["counts"] == {"downloaded": 2, "skipped": 0, "buy_on_site": 0, "failed": 2, "open_on_site": 0}
    assert len(w.plx) == 1 and w.gallery.fetched_ids() == [101] and w.ids() == ["3"]
    assert sorted(names(tmp_path)) == ["20260925-093000_cam_101.jpg", "20260925-093000_cam_xxpie-3.jpg"]
    assert all("failed: pailixiang API unavailable" in e for e in status["errors"][:2])
    assert status["errors"][-1].startswith("pailixiang API unavailable")
    rows = list(csv.DictReader(io.StringIO((out_dir(tmp_path).parent / "photos.csv").read_text("utf-8-sig"))))
    assert sorted(r["status"].split(":")[0] for r in rows) == ["downloaded", "downloaded", "failed", "failed"]


def test_outage_of_the_last_platform_ends_the_job_as_before(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [xxp(3), plx(1), plx(2)])
    w.forced["plx"] = lambda req: httpx.Response(401)
    status = run(api)
    assert status["state"] == "error" and status["done"] == 1 and len(w.plx) == 1
    assert status["errors"][-1].startswith("pailixiang API unavailable")


def test_viewer_download_for_both_platforms_and_csv_zip(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(1), xxp(2)])
    assert api.get("/api/facets").json()["originals"] is True
    one, two = (conn.execute("select id from photos where relpath = ?", (r,)).fetchone()[0]
                for r in ("1.jpg", "2.jpg"))
    for pid, expect in ((one, jpeg("1")), (two, jpeg("2"))):
        res = api.post(f"/api/photos/{pid}/original", json={"profile_id": ME})
        assert res.status_code == 200 and res.content == expect and res.headers["content-type"] == "image/jpeg"
    before = len(w.images)
    assert api.post(f"/api/photos/{one}/original", json={"profile_id": ME}).status_code == 200
    assert len(w.images) == before
    csv_path = out_dir(tmp_path).parent / "photos.csv"
    rows = {r["source_photo_id"]: r for r in csv.DictReader(io.StringIO(csv_path.read_text("utf-8-sig")))}
    assert {r["status"] for r in rows.values()} == {"downloaded"} and all(r["original_path"] for r in rows.values())
    z = zipfile.ZipFile(io.BytesIO(api.get("/api/originals/zip", params={"profile_id": ME}).content))
    assert sorted(z.namelist()) == sorted([*(f"originals/{n}" for n in names(tmp_path)), "photos.csv"])


def test_single_viewer_download_keeps_the_502_for_an_outage(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(1)])
    w.forced["plx"] = lambda req: httpx.Response(401)
    res = api.post("/api/photos/1/original", json={"profile_id": ME})
    assert res.status_code == 502 and res.json()["detail"].startswith("pailixiang API unavailable")


def test_validity_rules_per_platform(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(shaped("1"))
    assert originals.valid(p, "xxpie") and originals.valid(p, "photoplus")
    assert not originals.valid(p, "pailixiang") and not originals.valid(p, "yipai")
    p.write_bytes(jpeg("1"))
    assert originals.valid(p, "pailixiang")


def test_xxpie_registration_is_paced_like_any_other_request(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [xxp(7)])
    w.bad_codes = 1
    assert run(api)["counts"]["downloaded"] == 1
    events = sorted([(n, "reg") for n in w.registrations] + [(n, "search") for n, _, _ in w.xx])
    assert [k for _, k in events] == ["reg", "search", "reg", "search"]
    assert all(b - a >= xxpie.LOOKUP_GAP - 1e-6 for (a, _), (b, _) in zip(events, events[1:]))


def test_xxpie_registers_at_most_twice_per_lookup_when_every_answer_is_an_error(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [xxp(7)])
    w.bad_codes = 99
    status = run(api)
    assert status["state"] == "error" and len(w.registrations) == 2
    assert status["errors"][-1].startswith("xxpie API unavailable")


def test_every_row_of_a_down_platform_is_failed_even_when_the_job_stops_early(tmp_path, monkeypatch):
    c, conn, t, w, api = setup(tmp_path, monkeypatch, [plx(1), (YP, "101", "IMG_101.JPG", SHOT), plx(2)])
    w.forced["plx"] = lambda req: httpx.Response(401)
    w.gallery.api = lambda req: httpx.Response(401)
    status = run(api)
    assert status["state"] == "error" and w.ids() == [] and len(w.plx) == 1
    rows = list(csv.DictReader(io.StringIO((out_dir(tmp_path).parent / "photos.csv").read_text("utf-8-sig"))))
    assert all(r["status"].startswith("failed: ") and "API unavailable" in r["status"] for r in rows)
    assert any("yipai360 API unavailable" in e for e in status["errors"])
    assert any("pailixiang API unavailable" in e for e in status["errors"])
