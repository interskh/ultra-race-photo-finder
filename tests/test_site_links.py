import csv

import httpx
import pytest

from photofinder import db, originals, sources
from photofinder.index.stages import scan
from photofinder.web import app as web
from test_originals import Gallery, jpeg, yipai_index
from test_race_scan import embed_all, make_race
from test_search import A, X, make_index
from test_web import RaceClient, ME, label, no_real_models  # noqa: F401
from test_yipai import FakeTime


@pytest.mark.parametrize("platform, site_id, fname, url, exact, hint", [
    ("yipai", "1001", "IMG_1.JPG", "https://www.yipai360.com/photolivepc/?orderId=1001", False,
     "search the full file name (with extension) in 通过照片名搜索 and press Enter"),
    ("pailixiang", "a123", "DSC_1.JPG", "https://live.pailixiang.com/album/a123", False,
     "look near the shot time (照片直播 order is loose); 照片信息 under a photo shows file name and shot time"),
    ("photoplus", "4567", "P1.JPG", "https://live.photoplus.cn/live/4567?accessFrom=live#/live", False,
     "open the group tab; the ⓘ icon under a photo shows its file name"),
    ("xxpie", "abc", "IMG_1.JPG", "https://www.xxpie.com/m/albumFilenameSearch?album_id=abc&search_word=IMG_1.JPG",
     True, None),
    ("xxpie", "abc", "终点 A&B/1.JPG",
     "https://www.xxpie.com/m/albumFilenameSearch?album_id=abc&search_word=%E7%BB%88%E7%82%B9%20A%26B%2F1.JPG",
     True, None),
])
def test_site_link_per_platform(platform, site_id, fname, url, exact, hint):
    assert sources.site_link(platform, site_id, fname) == {"url": url, "exact": exact, "find_by": fname, "hint": hint}


def test_site_link_needs_a_known_platform_and_site_id():
    assert sources.site_link("elsewhere", "1", "A.JPG") is None
    assert sources.site_link("yipai", None, "A.JPG") is None
    assert sources.site_link(None, "1", "A.JPG") is None


def indexed_race():
    r = make_race()
    conn = db.connect(r)
    scan(conn, r)
    embed_all(conn)
    return r, conn, {rel: i for i, rel in conn.execute("select id, relpath from photos")}


def detail(api, photo_id):
    res = api.get(f"/api/photos/{photo_id}", params={"profile_id": ME})
    assert res.status_code == 200, res.text
    body = res.json()
    return body["platform"], body["fname"], body["site"] and body["site"]["url"]


def test_photo_detail_has_fname_site_and_platform_per_album():
    r, conn, p = indexed_race()
    api = RaceClient(web.create_app(r))
    assert detail(api, p["albums/yipai-1001/photos/101.jpg"]) == \
        ("yipai", "IMG_101.JPG", "https://www.yipai360.com/photolivepc/?orderId=1001")
    assert detail(api, p["albums/yipai-1002/photos/101.jpg"]) == \
        ("yipai", "DSC_101.JPG", "https://www.yipai360.com/photolivepc/?orderId=1002")
    assert detail(api, p["albums/xxpie-abc/photos/late.jpg"]) == \
        ("xxpie", "X2.JPG", "https://www.xxpie.com/m/albumFilenameSearch?album_id=abc&search_word=X2.JPG")
    site = api.get(f"/api/photos/{p['albums/xxpie-abc/photos/late.jpg']}", params={"profile_id": ME}).json()["site"]
    assert site["exact"] is True and site["find_by"] == "X2.JPG"
    conn.execute("update photos set source_photo_id = 'gone' where relpath = 'albums/xxpie-abc/photos/abc.jpg'")
    conn.commit()
    assert detail(api, p["albums/xxpie-abc/photos/abc.jpg"]) == ("xxpie", None, None)
    first = conn.execute("select min(id) from persons").fetchone()[0]
    results = api.post("/api/search", json={"profile_id": ME, "persons": [first]}).json()["results"]
    assert results and not any({"site", "fname", "platform"} & set(x) for x in results)


def test_legacy_single_album_uses_the_manifest_order_id_and_plain_folders_have_no_site(tmp_path, monkeypatch):
    c, conn, ids = yipai_index(tmp_path, monkeypatch, [("A1.JPG", None, None)])
    photo = conn.execute("select id from photos").fetchone()[0]
    assert detail(RaceClient(web.create_app(c)), photo) == \
        ("yipai", "A1.JPG", "https://www.yipai360.com/photolivepc/?orderId=ORD")
    (tmp_path / "plain").mkdir()
    plain, conn2, _ = make_index(tmp_path / "plain", [(1, (0, 0, 50, 100), A, X)], photos=1)
    photo = conn2.execute("select id from photos").fetchone()[0]
    assert detail(RaceClient(web.create_app(plain)), photo) == (None, None, None)


def test_mixed_race_originals_download_yipai_and_mark_others_open_on_site(tmp_path, monkeypatch):
    monkeypatch.setattr(originals, "ORIGINAL_PLATFORMS", ("yipai", "photoplus"))
    r, conn, p = indexed_race()
    t = FakeTime()
    g = Gallery(t, order="1001")
    g.add(101, "IMG_101.JPG")
    fetcher = originals.Fetcher(httpx.Client(transport=httpx.MockTransport(g)), sleep=t.sleep, clock=t.clock)
    api = RaceClient(web.create_app(r, fetcher=fetcher))
    yp, xx = p["albums/yipai-1001/photos/101.jpg"], p["albums/xxpie-abc/photos/late.jpg"]
    person = dict(conn.execute("select photo_id, id from persons"))
    for photo in (yp, xx):
        label(api, person[photo], "me")
    assert api.get("/api/facets").json()["originals"] is True
    assert api.post("/api/originals", json={"profile_id": ME}).status_code == 200
    api.app.state.originals.thread.join(10)
    status = api.get("/api/originals").json()
    assert status["state"] == "done" and status["errors"] == []
    assert status["counts"] == {"downloaded": 1, "skipped": 0, "buy_on_site": 0, "failed": 0, "open_on_site": 1}
    assert [f for _, f, _ in g.lookups] == ["IMG_101"] and g.fetched_ids() == [101]
    folder = originals.profile_folder(r, "Me")
    assert [f.read_bytes() for f in (folder / "originals").iterdir()] == [jpeg(101)]
    with open(folder / "photos.csv", encoding="utf-8-sig") as f:
        rows = {row["source_photo_id"]: row for row in csv.DictReader(f)}
    assert (rows["late"]["status"], rows["late"]["original_file_name"], rows["late"]["site_url"]) == (
        "open on site", "X2.JPG", "https://www.xxpie.com/m/albumFilenameSearch?album_id=abc&search_word=X2.JPG")
    assert (rows["101"]["status"], rows["101"]["site_url"]) == (
        "downloaded", "https://www.yipai360.com/photolivepc/?orderId=1001")
    mine = {x["photo_id"]: x for x in api.get("/api/me", params={"profile_id": ME}).json()["photos"]}
    assert (mine[xx]["original"], mine[xx]["platform"], mine[xx]["fname"]) == ("open on site", "xxpie", "X2.JPG")
    assert (mine[yp]["original"], mine[yp]["platform"], mine[yp]["site"]["exact"]) == ("downloaded", "yipai", False)
    res = api.post(f"/api/photos/{xx}/original", json={"profile_id": ME})
    assert res.status_code == 409 and res.json()["detail"].startswith("open on site")
    assert len(g.lookups) == 1 and len(g.fetched) == 1


def test_non_yipai_photo_never_takes_a_colliding_yipai_original(tmp_path, monkeypatch):
    monkeypatch.setattr(originals, "ORIGINAL_PLATFORMS", ("yipai", "photoplus"))
    r, conn, p = indexed_race()
    t = FakeTime()
    g = Gallery(t, order="1001")
    fetcher = originals.Fetcher(httpx.Client(transport=httpx.MockTransport(g)), sleep=t.sleep, clock=t.clock)
    api = RaceClient(web.create_app(r, fetcher=fetcher))
    xx = p["albums/xxpie-abc/photos/late.jpg"]
    label(api, dict(conn.execute("select photo_id, id from persons"))[xx], "me")
    row = originals.rows_of(r, list(web.photo_meta(conn, [xx]).values()))[0]
    folder = originals.profile_folder(r, "Me")
    (folder / "originals").mkdir(parents=True)
    cached = folder / "originals" / row["file"]
    cached.write_bytes(jpeg(101))
    res = api.post(f"/api/photos/{xx}/original", json={"profile_id": ME})
    assert res.status_code == 409 and res.json()["detail"].startswith("open on site")
    assert api.post("/api/originals", json={"profile_id": ME}).status_code == 200
    api.app.state.originals.thread.join(10)
    status = api.get("/api/originals").json()
    assert status["counts"] == {"downloaded": 0, "skipped": 0, "buy_on_site": 0, "failed": 0, "open_on_site": 1}
    with open(folder / "photos.csv", encoding="utf-8-sig") as f:
        assert [(x["status"], x["original_path"]) for x in csv.DictReader(f)] == [("open on site", "")]
    assert cached.read_bytes() == jpeg(101) and (g.lookups, g.fetched) == ([], [])


def test_old_status_belongs_to_the_row_with_the_same_id_and_file_name(tmp_path):
    base = {"photographer": None, "taken_at": None, "album": None, "group": None, "preview": "p", "file": "f.jpg"}
    rows = [{**base, "photo_id": 1, "source_photo_id": "101", "fname": "IMG_101.JPG", "platform": "yipai",
             "site": None},
            {**base, "photo_id": 2, "source_photo_id": "101", "fname": "P1.JPG", "platform": "elsewhere",
             "site": None},
            {**base, "photo_id": 3, "source_photo_id": "102", "fname": None, "platform": "yipai", "site": None}]
    folder = tmp_path / "out"
    originals.write_csv(folder, rows, {1: "failed: HTTP 503", 3: "failed: not in the gallery manifest"})
    assert originals.statuses(folder, rows) == {1: "failed: HTTP 503", 2: "open on site",
                                                3: "failed: not in the gallery manifest"}
    assert originals.statuses(folder, [rows[0] | {"fname": "IMG_1.JPG"}]) == {1: None}


def test_yipai_status_survives_a_later_non_yipai_row_with_the_same_id_and_file_name(tmp_path):
    base = {"photographer": None, "taken_at": None, "album": None, "group": None, "preview": "p", "file": "f.jpg",
            "source_photo_id": "101", "fname": "IMG_101.JPG", "site": None}
    rows = [{**base, "photo_id": 1, "platform": "yipai"}, {**base, "photo_id": 2, "platform": "elsewhere"}]
    folder = tmp_path / "out"
    originals.write_csv(folder, rows, {1: "failed: HTTP 503"})
    assert originals.statuses(folder, rows) == {1: "failed: HTTP 503", 2: "open on site"}


def test_open_on_site_status_ignores_stale_csv_and_files(tmp_path, monkeypatch):
    monkeypatch.setattr(originals, "ORIGINAL_PLATFORMS", ("yipai", "photoplus"))
    r, conn, p = indexed_race()
    rows = originals.rows_of(r, list(web.photo_meta(conn, [p["albums/xxpie-abc/photos/late.jpg"]]).values()))
    folder = tmp_path / "out"
    (folder / "originals").mkdir(parents=True)
    (folder / "originals" / rows[0]["file"]).write_bytes(jpeg(1))
    (folder / "photos.csv").write_text("source_photo_id,status\nlate,failed: not in the gallery manifest\n",
                                       encoding="utf-8-sig")
    assert originals.statuses(folder, rows) == {rows[0]["photo_id"]: "open on site"}


def test_yipai_and_photoplus_rows_with_the_same_id_and_file_name_keep_separate_statuses(tmp_path):
    base = {"photographer": None, "taken_at": None, "album": None, "group": None, "preview": "p", "file": "f.jpg",
            "source_photo_id": "101", "fname": "IMG_101.JPG"}
    rows = [{**base, "photo_id": 1, "platform": "yipai", "file": "a.jpg",
             "site": {"url": "https://www.yipai360.com/photolivepc/?orderId=1"}},
            {**base, "photo_id": 2, "platform": "photoplus", "file": "b.jpg",
             "site": {"url": "https://live.photoplus.cn/live/9?accessFrom=live#/live"}}]
    folder = tmp_path / "out"
    originals.write_csv(folder, rows, {1: "failed: HTTP 503", 2: "failed: not found in the photoplus album"})
    assert originals.statuses(folder, rows) == {1: "failed: HTTP 503",
                                                2: "failed: not found in the photoplus album"}
