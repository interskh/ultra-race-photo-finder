import hashlib
import io
import itertools
import json
import logging
import math
import sqlite3
import threading
from collections import Counter
from pathlib import Path

import httpx
import pytest
from PIL import Image

from photofinder import cli, config, db, races
from photofinder.index.stages import scan
from photofinder.sources import photoplus
from photofinder.sources.base import AlbumDownloader
from photofinder.sources.common import RetriesExhausted, site_key

FIXTURES = Path(__file__).parent / "fixtures"
DETAIL = json.loads((FIXTURES / "photoplus_detail.json").read_text(encoding="utf-8"))
ALBUMS = json.loads((FIXTURES / "photoplus_albums.json").read_text(encoding="utf-8"))
LISTING = json.loads((FIXTURES / "photoplus_list.json").read_text(encoding="utf-8"))
ONE = json.loads((FIXTURES / "photoplus_album_one.json").read_text(encoding="utf-8"))
ACTIVITY = "89243825"
URL = f"https://live.photoplus.cn/live/{ACTIVITY}?accessFrom=live#/live"
KEY = f"photoplus-{ACTIVITY}"
TITLE = "ACG崇礼168超级越野赛"
LIST_PHOTOS = LISTING["result"]["pics_array"]
ONE_PHOTOS = ONE["result"]["pics"]
buf = io.BytesIO()
Image.effect_noise((200, 150), 64).convert("RGB").save(buf, "JPEG")
PREVIEW = buf.getvalue()


def photo(i):
    return dict(ONE_PHOTOS[0], id=770000000 + i, pic_name=f"DSC{i:05d}.jpg",
                big_img=f"//pb.plusx.cn/plus/immediate/{ACTIVITY}/{i}.jpg")


def pics(*ids):
    return [photo(i) for i in ids]


class FakePP:
    def __init__(self, albums=(), photos=None):
        self.albums = [(1347353 + n, name, ps) for n, (name, ps) in enumerate(albums)]
        self.photos = photos if photos is not None else list(
            {p["id"]: p for _, _, ps in self.albums for p in ps}.values())
        self.pics_total = lambda page: len(self.photos)
        self.status = 200
        self.calls = []
        self.bad_codes = 0
        self.listings = 0
        self.expire_first_listing = False
        self.upload_on_expiry = None
        self.expire_next = 0
        self.expired_signs = set()
        self.images = Counter()
        self._lock = threading.Lock()

    def signed(self, ps):
        self.listings += 1
        return [dict(p, big_img=f"{p['big_img']}?sign=L{self.listings}") for p in ps]

    def __call__(self, req):
        if req.url.host == "pb.plusx.cn":
            with self._lock:
                self.images[req.url.path] += 1
            with self._lock:
                sign = req.url.params["sign"]
                if sign not in self.expired_signs and self.expire_next:
                    self.expire_next -= 1
                    self.expired_signs.add(sign)
            if sign in self.expired_signs:
                return httpx.Response(403)
            if self.expire_first_listing and req.url.params["sign"] == "L1":
                with self._lock:
                    if self.upload_on_expiry is not None:
                        for ps in [*(ps for _, _, ps in self.albums), self.photos]:
                            ps.insert(0, self.upload_on_expiry)
                        self.upload_on_expiry = None
                return httpx.Response(403)
            return httpx.Response(200, content=PREVIEW)
        q = dict(req.url.params)
        self.calls.append((req.url.path, q))
        if self.status != 200:
            return httpx.Response(self.status)
        s = q.pop("_s")
        text = "&".join(f"{k}={v}" for k, v in sorted(q.items())) + site_key("photoplus_salt")
        if self.bad_codes or hashlib.md5(text.encode()).hexdigest() != s:
            self.bad_codes = max(0, self.bad_codes - 1)
            return httpx.Response(200, json={"code": -1, "message": "请求参数不合法", "success": False})
        ok = {"code": 1, "message": "I'am OK", "success": True}
        if req.url.path == "/live/detail":
            return httpx.Response(200, json=DETAIL)
        if req.url.path == "/album/albums":
            return httpx.Response(200, json={**ok, "result": [
                dict(ALBUMS["result"][0], album_id=aid, name=name, pic_num=len(ps)) for aid, name, ps in self.albums]})
        page = int(q["page"])
        if req.url.path == "/album/one":
            aid, name, ps = next(a for a in self.albums if str(a[0]) == q["albumId"])
            return httpx.Response(200, json={**ok, "result": {
                "pageTotal": 1.0, "album": dict(ONE["result"]["album"], album_id=aid, name=name, pic_num=len(ps)),
                "pic_total": len(ps), "pics": self.signed(ps[(page - 1) * 200:page * 200])}})
        assert req.url.path == "/pic/list"
        return httpx.Response(200, json={**ok, "result": {
            "pageTotal": 2.0, "pics_total": self.pics_total(page), "view_count": 1,
            "pics_array": self.signed(self.photos[(page - 1) * 100:page * 100])}})

    def paths(self):
        return [(p, q.get("albumId"), q.get("page")) for p, q in self.calls]


def client_for(site):
    return httpx.Client(headers=photoplus.HEADERS, transport=httpx.MockTransport(site))


def adapter(site, **kw):
    return photoplus.Adapter(client_for(site), ACTIVITY, sleep=lambda s: None, **kw)


def walk(a):
    out, cursor = [], None
    while True:
        rows, cursor, total = a.list_page(cursor)
        out.append(rows)
        if cursor is None:
            return out, total


def groups(pages):
    return {r.source_id: r.group_name for rows in pages for r in rows}


def sid(i):
    return str(770000000 + i)


def test_signature_is_the_md5_of_the_sorted_unquoted_query_plus_the_salt():
    params = dict(activityNo=39352660, key="", isNew=False, count=100, page=1, size=2000, ppSign="")
    signed = photoplus.sign(params, 1790640755565)
    text = "_t=1790640755565&activityNo=39352660&count=100&isNew=false&key=&page=1&ppSign=&size=2000"
    assert signed["_s"] == hashlib.md5((text + "test-salt").encode()).hexdigest()
    assert photoplus.sign({**params, "activityNo": "39352660"}, 1790640755565)["_s"] == signed["_s"]
    assert signed["isNew"] == "false" and signed["_t"] == 1790640755565


def test_signature_skips_nulls():
    assert photoplus.sign({"a": 1, "b": None}, 5) == photoplus.sign({"a": 1}, 5)


def test_meta_reads_the_title_with_one_signed_request():
    site = FakePP()
    assert adapter(site).meta() == {"title": TITLE, "total": None}
    ((path, q),) = site.calls
    assert path == "/live/detail" and q["activityNo"] == ACTIVITY


def test_each_attempt_is_signed_with_a_fresh_time():
    site = FakePP()
    site.bad_codes = 2
    ticks = itertools.count(1790640756, 1)
    assert adapter(site, clock=lambda: next(ticks)).meta()["title"] == TITLE
    assert [q["_t"] for _, q in site.calls] == ["1790640756000", "1790640757000", "1790640758000"]


def test_list_query_is_the_free_watermarked_one():
    site = FakePP(photos=pics(1))
    assert len(adapter(site).list_page(None)[0]) == 1
    (q,) = [q for p, q in site.calls if p == "/pic/list"]
    assert {k: v for k, v in q.items() if k not in ("_t", "_s")} == {
        "activityNo": ACTIVITY, "key": "", "isNew": "false", "count": "100", "page": "1", "size": "2000",
        "ppSign": ""}


def test_rejected_signature_is_retried_then_gives_up():
    site = FakePP()
    site.bad_codes = 99
    with pytest.raises(RetriesExhausted):
        adapter(site, tries=3).meta()
    assert len(site.calls) == 3


def test_sub_albums_page_by_200_then_the_list_fills_the_gap():
    site = FakePP([("A", pics(*range(450))), ("B", pics(1000, 1001))], photos=pics(*range(450), 1000, 1001, 2000))
    a = adapter(site)
    pages, total = walk(a)
    assert site.paths() == [("/album/albums", None, None), ("/album/one", "1347353", "1"),
                            ("/album/one", "1347353", "2"), ("/album/one", "1347353", "3"),
                            ("/album/one", "1347354", "1")] + [("/pic/list", None, str(p)) for p in range(1, 6)]
    assert [len(rows) for rows in pages] == [200, 200, 50, 2, 100, 100, 100, 100, 53]
    g = groups(pages)
    assert len(g) == 453 and total == 453
    assert (g[sid(0)], g[sid(449)], g[sid(1001)], g[sid(2000)]) == ("A", "A", "B", None)


def test_a_photo_in_two_sub_albums_keeps_the_first_group():
    site = FakePP([("A", pics(1, 2)), ("B", pics(2, 3))])
    pages, _ = walk(adapter(site))
    assert [[(r.source_id, r.group_name) for r in rows] for rows in pages] == [
        [(sid(1), "A"), (sid(2), "A")], [(sid(2), "A"), (sid(3), "B")], [(sid(1), "A"), (sid(2), "A"), (sid(3), "B")]]


def test_list_phase_rows_carry_the_first_group_or_none():
    site = FakePP([("A", pics(1, 2))], photos=pics(9, 1, 8, 2))
    pages, total = walk(adapter(site))
    assert [[(r.source_id, r.group_name) for r in rows] for rows in pages] == [
        [(sid(1), "A"), (sid(2), "A")], [(sid(9), None), (sid(1), "A"), (sid(8), None), (sid(2), "A")]]
    assert total == 4


def test_list_phase_reads_to_the_last_page_even_when_every_photo_is_seen():
    site = FakePP([("A", pics(*range(150)))], photos=pics(*range(150)))
    walk(adapter(site))
    assert [p[2] for p in site.paths() if p[0] == "/pic/list"] == ["1", "2"]


def test_more_photos_than_pics_total_pages_the_list_to_a_short_page(caplog, tmp_path):
    caplog.set_level(logging.WARNING, logger="download")
    site = FakePP([("A", pics(*range(150)))], photos=pics(*range(250)))
    site.pics_total = lambda page: 100
    pages, total = walk(adapter(site))
    assert [p[2] for p in site.paths() if p[0] == "/pic/list"] == ["1", "2", "3"]
    assert len(groups(pages)) == 250 and total == 100
    assert [r.getMessage() for r in caplog.records if "pics_total" in r.getMessage()] == [
        "photoplus: seen 150 distinct photos vs pics_total 100; paging /pic/list to a short page"]
    d = AlbumDownloader(client_for(site), adapter(site), tmp_path, sleep=lambda s: None)
    try:
        assert d.run() == {"done": 250}
    finally:
        d.close()


def test_list_pages_follow_pics_total_not_page_total():
    site = FakePP(photos=pics(*range(250)))
    pages, total = walk(adapter(site))
    assert [p[2] for p in site.paths() if p[0] == "/pic/list"] == ["1", "2", "3"]
    assert sum(map(len, pages)) == 250 and total == 250


def test_a_later_zero_pics_total_does_not_clobber_the_total():
    site = FakePP(photos=pics(*range(250)))
    site.pics_total = lambda page: 250 if page == 1 else 0
    pages, total = walk(adapter(site))
    assert sum(map(len, pages)) == 250 and total == 250


def test_the_first_pics_total_is_kept_for_the_run():
    site = FakePP(photos=pics(*range(250)))
    site.pics_total = lambda page: 250 if page == 1 else 400
    pages, total = walk(adapter(site))
    assert [p[2] for p in site.paths() if p[0] == "/pic/list"] == ["1", "2", "3"]
    assert total == 250


@pytest.mark.parametrize("value", [None, "250", True])
def test_unusable_pics_total_pages_until_a_short_page(value):
    site = FakePP(photos=pics(*range(200)))
    site.pics_total = lambda page: value
    pages, total = walk(adapter(site))
    assert [p[2] for p in site.paths() if p[0] == "/pic/list"] == ["1", "2", "3"]
    assert sum(map(len, pages)) == 200 and total is None


@pytest.mark.parametrize("cursor", [None, ("album", 1, 1), ("list", 1)])
def test_same_cursor_relist_re_emits_that_page(cursor):
    site = FakePP([("A", pics(1, 2)), ("B", pics(2, 3))], photos=pics(3, 4))
    a = adapter(site)
    first = [r.source_id for r in a.list_page(cursor)[0]]
    again = a.list_page(cursor)[0]
    assert first and [r.source_id for r in again] == first
    assert all(r.url.endswith(f"?sign=L{site.listings}") for r in again)


def test_relist_keeps_the_first_group():
    site = FakePP([("A", pics(1, 2)), ("B", pics(2, 3))])
    a = adapter(site)
    a.list_page(None)
    assert [(r.source_id, r.group_name) for r in a.list_page(("album", 1, 1))[0]] == [(sid(2), "A"), (sid(3), "B")]
    assert [(r.source_id, r.group_name) for r in a.list_page(None)[0]] == [(sid(1), "A"), (sid(2), "A")]
    assert site.paths().count(("/album/albums", None, None)) == 1


def test_album_without_sub_albums_downloads_from_the_list():
    site = FakePP(photos=pics(1, 2, 3))
    pages, total = walk(adapter(site))
    assert [p[0] for p in site.paths()] == ["/album/albums", "/pic/list"]
    assert groups(pages) == {sid(1): None, sid(2): None, sid(3): None} and total == 3


def test_rows_map_from_the_real_sub_album_listing():
    r = photoplus.to_row(ONE_PHOTOS[0], "放松跑")
    assert (r.source_id, r.fname, r.photographer_uid, r.photographer, r.group_name, r.taken_at, r.width, r.height) == (
        "978753553", "FEC18110.jpg", "9100000001", "Camera 1", "放松跑", "2026-07-12 08:11:19", 4000, 2667)
    assert r.url == "https:" + ONE_PHOTOS[0]["big_img"]
    assert r.url.startswith("https://pb.plusx.cn/plus/immediate/89243825/")


def test_rows_without_camer_credit_the_retoucher():
    r = photoplus.to_row(LIST_PHOTOS[0], None)
    assert (r.source_id, r.fname, r.photographer_uid, r.photographer, r.taken_at) == (
        "622140671", "微信图片_5681-94-91_797886_107.jpg", "9900000001", "Photographer B", "2026-07-12 23:44:56")


def test_absolute_preview_url_is_kept():
    assert photoplus.to_row(dict(LIST_PHOTOS[0], big_img="https://x/y.jpg"), None).url == "https://x/y.jpg"


@pytest.mark.parametrize("value", ["2026-07-12T08:11:19", "2026-07-12 08:11", "2026-07-12 08:11:19.5", "", None,
                                   9881274224, "2026-07-12 08:11:19\n"])
def test_unusable_relate_time_is_none(value):
    assert photoplus.to_row(dict(LIST_PHOTOS[0], relate_time=value), None).taken_at is None


def test_unsafe_or_missing_ids_are_skipped_and_leave_the_total_honest():
    no_id = {k: v for k, v in photo(3).items() if k != "id"}
    bad = [dict(photo(1), id="../evil"), no_id]
    site = FakePP([("A", [*bad, photo(2)])], photos=[*bad, photo(2)])
    pages, total = walk(adapter(site))
    assert [[r.source_id for r in rows] for rows in pages] == [[sid(2)], [sid(2)]]
    assert total == 1


def test_downloader_fetches_every_photo_once_and_a_rerun_fetches_nothing(tmp_path):
    site = FakePP([("A", pics(*range(250))), ("B", pics(5, 300))], photos=pics(*range(250), 300, 400))
    for _ in range(2):
        d = AlbumDownloader(client_for(site), adapter(site), tmp_path, sleep=lambda s: None)
        try:
            assert d.run() == {"done": 252}
        finally:
            d.close()
    assert sum(site.images.values()) == 252 and set(site.images.values()) == {1}
    m = sqlite3.connect(tmp_path / "manifest.sqlite")
    assert dict(m.execute("select key, value from meta where key = 'title'")) == {"title": TITLE}
    assert dict(m.execute(f"select source_id, group_name from catalog where source_id in "
                          f"('{sid(5)}', '{sid(300)}', '{sid(400)}')")) == {sid(5): "A", sid(300): "B", sid(400): None}


def test_expired_urls_are_relisted_and_keep_their_group(tmp_path):
    site = FakePP([("A", pics(1, 2)), ("B", pics(2, 3))])
    site.expire_first_listing = True
    d = AlbumDownloader(client_for(site), adapter(site), tmp_path, sleep=lambda s: None)
    try:
        assert d.run() == {"done": 3}
    finally:
        d.close()
    m = sqlite3.connect(tmp_path / "manifest.sqlite")
    assert dict(m.execute("select source_id, group_name from catalog")) == {sid(1): "A", sid(2): "A", sid(3): "B"}


def test_a_photo_shifted_to_the_next_page_before_the_relist_is_still_fetched(tmp_path):
    site = FakePP([("A", pics(*range(250)))])
    site.expire_first_listing = True
    site.upload_on_expiry = photo(9999)
    d = AlbumDownloader(client_for(site), adapter(site), tmp_path, sleep=lambda s: None)
    try:
        assert d.run() == {"done": 251}
    finally:
        d.close()
    m = sqlite3.connect(tmp_path / "manifest.sqlite")
    assert m.execute("select status, group_name, error from catalog where source_id=?", (sid(199),)).fetchone() == (
        "done", "A", None)
    assert site.images[f"/plus/immediate/{ACTIVITY}/199.jpg"] == 2


def test_downloader_reports_photos_missing_from_the_listing(tmp_path):
    site = FakePP(photos=pics(*range(70)))
    site.pics_total = lambda page: 75
    d = AlbumDownloader(client_for(site), adapter(site), tmp_path, sleep=lambda s: None)
    try:
        assert d.run() == {"done": 70, "missing": 5}
    finally:
        d.close()


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(config, "DATA_ROOT", root)
    return root


def test_download_then_scan_uses_the_catalog(data_root):
    races.add_race("2026-x", "X")
    races.add_album("2026-x", URL, TITLE)
    site = FakePP([("放松跑", ONE_PHOTOS)], photos=[*ONE_PHOTOS, *LIST_PHOTOS])
    d = AlbumDownloader(client_for(site), adapter(site), races.album_dir("2026-x", KEY), sleep=lambda s: None)
    try:
        assert d.run() == {"done": 6}
    finally:
        d.close()
    r = races.race_dir("2026-x")
    conn = db.connect(r)
    assert scan(conn, r)["new"] == 6
    got = conn.execute("select relpath, taken_at, album, album_key, photographer_uid, photographer, grp from photos "
                       "where source_photo_id = '978753553'").fetchone()
    assert tuple(got) == (f"albums/{KEY}/photos/978753553.jpg", "2026-07-12 08:11:19", TITLE, KEY,
                          "photoplus:9100000001", "Camera 1", "放松跑")


@pytest.fixture
def site(data_root, monkeypatch):
    site = FakePP([("A", pics(1, 2))], photos=pics(1, 2, 3))
    monkeypatch.setattr(cli, "album_client", lambda platform: client_for(site))
    races.add_race("2026-x", "X")
    return site


def registry(root):
    return json.loads((root / "races.json").read_text(encoding="utf-8"))


def test_album_add_fetches_the_title_from_the_users_url(data_root, site, capsys):
    cli.main(["album", "add", "2026-x", URL])
    assert registry(data_root)["races"][0]["albums"] == [
        {"key": KEY, "platform": "photoplus", "site_id": ACTIVITY, "url": URL, "title": TITLE}]
    assert [p for p, _ in site.calls] == ["/live/detail"]
    assert KEY in capsys.readouterr().out


def test_album_add_title_fetch_failure_suggests_title(data_root, site):
    site.status = 403
    with pytest.raises(SystemExit) as e:
        cli.main(["album", "add", "2026-x", URL])
    assert "--title" in str(e.value.code)
    assert registry(data_root)["races"][0]["albums"] == []


def test_cli_download_runs_the_photoplus_album(data_root, site, monkeypatch):
    monkeypatch.setattr(cli, "album_downloader",
                        lambda client, adapter, out_dir: AlbumDownloader(client, adapter, out_dir, sleep=lambda s: None))
    races.add_album("2026-x", URL, TITLE)
    cli.main(["download", "2026-x"])
    photos = races.album_dir("2026-x", KEY) / "photos"
    assert sorted(p.name for p in photos.iterdir()) == sorted(f"{sid(i)}.jpg" for i in (1, 2, 3))


def run_full_or_quick(site, out, full=False, **kw):
    d = AlbumDownloader(client_for(site), adapter(site), out, sleep=lambda s: None, **kw)
    try:
        return d.run(full=full)
    finally:
        d.close()


def listing_requests(site):
    return [p for p, _ in site.calls if p in ("/album/albums", "/album/one", "/pic/list")]


def manifest(out):
    return sqlite3.connect(out / "manifest.sqlite")


def quick_site():
    return FakePP([("A", pics(*range(250))), ("B", pics(300, 301))], photos=pics(*range(250), 300, 301, 400))


def test_full_run_stores_complete_snapshots_and_counts(tmp_path):
    site = quick_site()
    assert run_full_or_quick(site, tmp_path) == {"done": 253}
    m = manifest(tmp_path)
    a, b = (f"album:{site.albums[i][0]}" for i in (0, 1))
    assert dict(m.execute("select key, value from meta where key like 'count:%'")) == {
        f"count:{a}": "250", f"count:{b}": "2", "count:list": "253"}
    assert json.loads(m.execute("select value from meta where key='listing:albums'").fetchone()[0]) == [a, b]

    def ids(key):
        return [r[0] for r in m.execute("select source_id from listing where list_key=? order by pos", (key,))]
    assert ids(a) == [sid(i) for i in range(250)] and ids(b) == [sid(300), sid(301)]
    assert ids("list") == [p["id"] and str(p["id"]) for p in site.photos]
    assert [p[2] for p in site.paths() if p[0] == "/pic/list"] == ["1", "2", "3"]


def test_unchanged_second_run_makes_two_listing_requests_and_downloads_nothing(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="download")
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    site.calls.clear()
    site.images.clear()
    assert run_full_or_quick(site, tmp_path) == {"done": 253}
    assert listing_requests(site) == ["/album/albums", "/pic/list"]
    assert [q["page"] for p, q in site.calls if p == "/pic/list"] == ["1"]
    assert not site.images
    assert "photoplus: unchanged since last check: 3 listings, 253 photos" in [r.getMessage() for r in caplog.records]


def test_a_changed_sub_album_count_diffs_only_the_changed_listings(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="download")
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    site.calls.clear()
    site.images.clear()
    site.albums[1][2].append(photo(302))
    site.photos.append(photo(302))
    assert run_full_or_quick(site, tmp_path) == {"done": 254}
    assert site.paths()[1:] == [("/album/albums", None, None), ("/pic/list", None, "1"),
                                ("/album/one", str(site.albums[1][0]), "1"), ("/pic/list", None, "3")]
    assert list(site.images) == [f"/plus/immediate/{ACTIVITY}/302.jpg"]
    messages = [r.getMessage() for r in caplog.records]
    assert f"photoplus: album:{site.albums[1][0]} changed 2 -> 3" in messages
    assert "photoplus: list changed 253 -> 254" in messages
    assert any(m.startswith(f"photoplus: album:{site.albums[1][0]}: 3 photos by linear read") or
               m.startswith(f"photoplus: album:{site.albums[1][0]}: 3 photos by bisect") for m in messages)
    assert manifest(tmp_path).execute("select group_name from catalog where source_id=?", (sid(302),)).fetchone() == ("B",)
    site.calls.clear()
    run_full_or_quick(site, tmp_path)
    assert listing_requests(site) == ["/album/albums", "/pic/list"]


def test_a_changed_list_count_alone_diffs_the_list(tmp_path):
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    site.calls.clear()
    site.photos.append(photo(500))
    assert run_full_or_quick(site, tmp_path) == {"done": 254}
    assert listing_requests(site) == ["/album/albums", "/pic/list", "/pic/list"]
    assert manifest(tmp_path).execute("select group_name from catalog where source_id=?", (sid(500),)).fetchone() == (None,)


def test_a_new_sub_album_is_read_linearly_and_grouped(tmp_path):
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    site.calls.clear()
    site.albums.append((site.albums[-1][0] + 1, "C", pics(600, 601, 602)))
    site.photos.extend(pics(600, 601, 602))
    assert run_full_or_quick(site, tmp_path) == {"done": 256}
    assert listing_requests(site) == ["/album/albums", "/pic/list", "/album/one", "/pic/list"]
    m = manifest(tmp_path)
    assert len(json.loads(m.execute("select value from meta where key='listing:albums'").fetchone()[0])) == 3
    assert [r[0] for r in m.execute("select group_name from catalog where source_id in (?,?,?)",
                                    (sid(600), sid(601), sid(602)))] == ["C"] * 3
    assert m.execute("select count(*) from listing where list_key=?", (f"album:{site.albums[2][0]}",)).fetchone()[0] == 3
    site.calls.clear()
    run_full_or_quick(site, tmp_path)
    assert listing_requests(site) == ["/album/albums", "/pic/list"]


def test_a_removed_sub_album_drops_its_snapshot(tmp_path):
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    gone = f"album:{site.albums[1][0]}"
    site.calls.clear()
    del site.albums[1]
    run_full_or_quick(site, tmp_path)
    assert listing_requests(site) == ["/album/albums", "/pic/list"]
    m = manifest(tmp_path)
    assert m.execute("select count(*) from listing where list_key=?", (gone,)).fetchone()[0] == 0
    assert m.execute("select count(*) from meta where key=?", (f"count:{gone}",)).fetchone()[0] == 0
    assert json.loads(m.execute("select value from meta where key='listing:albums'").fetchone()[0]) == [
        f"album:{site.albums[0][0]}"]
    site.calls.clear()
    run_full_or_quick(site, tmp_path)
    assert listing_requests(site) == ["/album/albums", "/pic/list"]


def test_a_failed_catalog_row_forces_a_full_listing(tmp_path):
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    m = manifest(tmp_path)
    m.execute("update catalog set status='failed', error='HTTP 403', file=null where source_id=?", (sid(7),))
    m.commit()
    (tmp_path / "photos" / f"{sid(7)}.jpg").unlink()
    site.calls.clear()
    assert run_full_or_quick(site, tmp_path) == {"done": 253}
    assert len(listing_requests(site)) > 5
    assert site.images[f"/plus/immediate/{ACTIVITY}/7.jpg"] == 2


def test_full_flag_lists_everything_even_when_unchanged(tmp_path):
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    site.calls.clear()
    run_full_or_quick(site, tmp_path, full=True)
    assert len(listing_requests(site)) > 5


def test_an_interrupted_full_run_leaves_no_current_snapshot(tmp_path):
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    site.calls.clear()
    site.photos.append(photo(500))
    real = site.__call__
    state = {"list_pages": 0}

    def flaky(req):
        if req.url.path == "/pic/list":
            state["list_pages"] += 1
            if state["list_pages"] == 3:
                raise RuntimeError("network down")
        return real(req)
    d = AlbumDownloader(client_for(flaky), adapter(site), tmp_path, sleep=lambda s: None)
    d.adapter.client = client_for(flaky)
    with pytest.raises(Exception):
        d.run(full=True)
    d.close()
    m = manifest(tmp_path)
    assert m.execute("select count(*) from meta where key='listing:albums'").fetchone()[0] == 0
    site.calls.clear()
    assert run_full_or_quick(site, tmp_path) == {"done": 254}
    assert len(listing_requests(site)) > 5
    assert m.execute("select value from meta where key='count:list'").fetchone()[0] == "254"


def test_a_partial_listing_never_replaces_a_complete_snapshot(tmp_path):
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    m = manifest(tmp_path)
    before = m.execute("select count(*) from listing where list_key='list'").fetchone()[0]
    real = site.__call__

    def flaky(req):
        if req.url.path == "/pic/list" and req.url.params["page"] == "2":
            raise RuntimeError("network down")
        return real(req)
    site.photos.append(photo(500))
    d = AlbumDownloader(client_for(flaky), adapter(site), tmp_path, sleep=lambda s: None)
    d.adapter.client = client_for(flaky)
    with pytest.raises(Exception):
        d.run(full=True)
    d.close()
    assert m.execute("select count(*) from listing where list_key='list'").fetchone()[0] == before


def test_a_first_run_interrupted_before_the_end_has_no_snapshot_marker(tmp_path):
    site = quick_site()
    real = site.__call__

    def flaky(req):
        if req.url.path == "/pic/list" and req.url.params["page"] == "3":
            raise RuntimeError("network down")
        return real(req)
    d = AlbumDownloader(client_for(flaky), adapter(site), tmp_path, sleep=lambda s: None)
    d.adapter.client = client_for(flaky)
    with pytest.raises(Exception):
        d.run()
    d.close()
    site.calls.clear()
    run_full_or_quick(site, tmp_path)
    assert len(listing_requests(site)) > 5


def test_a_listing_with_an_unusable_count_is_never_skipped(tmp_path):
    site = quick_site()
    site.pics_total = lambda page: None
    run_full_or_quick(site, tmp_path)
    site.calls.clear()
    run_full_or_quick(site, tmp_path)
    assert len(listing_requests(site)) > 5


def test_cli_download_full_flag_is_parsed_and_passed_through(data_root, site, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "download_album", lambda album, out_dir, full=False: seen.append(full) or {"done": 1})
    races.add_album("2026-x", URL, TITLE)
    cli.main(["download", "2026-x"])
    cli.main(["download", "2026-x", KEY, "--full"])
    assert seen == [False, True]


def test_cli_download_full_reaches_the_photoplus_run(data_root, site, monkeypatch):
    monkeypatch.setattr(cli, "album_downloader",
                        lambda client, adapter, out_dir: AlbumDownloader(client, adapter, out_dir, sleep=lambda s: None))
    races.add_album("2026-x", URL, TITLE)
    cli.main(["download", "2026-x"])
    site.calls.clear()
    cli.main(["download", "2026-x"])
    assert listing_requests(site) == ["/album/albums", "/pic/list"]
    site.calls.clear()
    cli.main(["download", "2026-x", "--full"])
    assert len(listing_requests(site)) > 2


def big_site(n=3000, albums=()):
    return FakePP(albums, photos=pics(*range(n)))


def first_run(site, tmp_path):
    run_full_or_quick(site, tmp_path)
    site.calls.clear()
    site.images.clear()


def fetched(site):
    return sorted(int(p.rsplit("/", 1)[1].split(".")[0]) for p in site.images)


def list_pages_read(site):
    return [int(q["page"]) for p, q in site.calls if p == "/pic/list"]


def snapshot(tmp_path, key="list"):
    return [r[0] for r in manifest(tmp_path).execute("select source_id from listing where list_key=? order by pos", (key,))]


def remote_ids(site):
    return [str(p["id"]) for p in site.photos]


def test_a_cluster_of_adds_in_a_long_list_costs_a_few_page_reads(tmp_path):
    site = big_site()
    first_run(site, tmp_path)
    site.photos[1500:1500] = pics(*range(5000, 5005))
    assert run_full_or_quick(site, tmp_path) == {"done": 3005}
    assert fetched(site) == list(range(5000, 5005))
    assert len(list_pages_read(site)) <= 2 + 2 * 5
    assert snapshot(tmp_path) == remote_ids(site)


@pytest.mark.parametrize("at", [0, 1, 99, 100, 1500, 2999, 3000])
def test_adds_at_the_top_middle_and_bottom(tmp_path, at):
    site = big_site()
    first_run(site, tmp_path)
    site.photos[at:at] = pics(7001, 7002)
    assert run_full_or_quick(site, tmp_path) == {"done": 3002}
    assert fetched(site) == [7001, 7002]
    assert len(list_pages_read(site)) <= 12
    assert snapshot(tmp_path) == remote_ids(site)
    site.calls.clear()
    run_full_or_quick(site, tmp_path)
    assert listing_requests(site) == ["/album/albums", "/pic/list"]


def test_a_whole_page_of_adds_at_the_top_still_bisects(tmp_path):
    site = big_site()
    first_run(site, tmp_path)
    site.photos[0:0] = pics(*range(8000, 8100))
    run_full_or_quick(site, tmp_path)
    assert fetched(site) == list(range(8000, 8100))
    assert len(list_pages_read(site)) <= 4
    assert snapshot(tmp_path) == remote_ids(site)


def test_a_whole_page_of_adds_in_the_middle_bisects(tmp_path):
    site = big_site()
    first_run(site, tmp_path)
    site.photos[1500:1500] = pics(*range(8000, 8100))
    run_full_or_quick(site, tmp_path)
    assert fetched(site) == list(range(8000, 8100))
    assert len(list_pages_read(site)) <= 2 + 2 * 5
    assert snapshot(tmp_path) == remote_ids(site)


def test_scattered_adds_never_cost_more_than_reading_every_page(tmp_path):
    site = big_site()
    first_run(site, tmp_path)
    new = []
    for k, at in enumerate(range(2950, 0, -150)):
        site.photos.insert(at, photo(9000 + k))
        new.append(9000 + k)
    run_full_or_quick(site, tmp_path)
    assert fetched(site) == sorted(new)
    reads = list_pages_read(site)
    assert len(reads) <= 1 + math.ceil(len(site.photos) / 100)
    assert len(set(reads[1:])) == len(reads) - 1
    assert snapshot(tmp_path) == remote_ids(site)


def test_a_deleted_photo_is_recorded_and_kept_locally(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="download")
    site = big_site(500)
    first_run(site, tmp_path)
    removed = site.photos.pop(250)
    assert run_full_or_quick(site, tmp_path) == {"done": 500}
    m = manifest(tmp_path)
    assert [r[0] for r in m.execute("select source_id from gone")] == [str(removed["id"])]
    assert m.execute("select status from catalog where source_id=?", (str(removed["id"]),)).fetchone() == ("done",)
    assert (tmp_path / "photos" / f"{removed['id']}.jpg").exists()
    assert not site.images
    assert snapshot(tmp_path) == remote_ids(site)
    assert "photoplus: 1 photos were removed from the album since the last check; kept locally" in [
        r.getMessage() for r in caplog.records]
    site.photos.insert(250, removed)
    run_full_or_quick(site, tmp_path)
    assert m.execute("select count(*) from gone").fetchone()[0] == 0
    assert not site.images


def test_deletions_in_a_sub_album_alone_are_not_recorded(tmp_path):
    site = FakePP([("A", pics(*range(300)))], photos=pics(*range(300)))
    first_run(site, tmp_path)
    site.albums[0][2].pop(10)
    run_full_or_quick(site, tmp_path)
    assert manifest(tmp_path).execute("select count(*) from gone").fetchone()[0] == 0
    assert manifest(tmp_path).execute("select count(*) from listing where list_key=?",
                                      (f"album:{site.albums[0][0]}",)).fetchone()[0] == 299


def test_an_add_and_a_delete_in_different_spans(tmp_path):
    site = big_site()
    first_run(site, tmp_path)
    removed = site.photos.pop(2000)
    site.photos[500:500] = pics(7001, 7002)
    assert run_full_or_quick(site, tmp_path) == {"done": 3002}
    assert fetched(site) == [7001, 7002]
    assert [r[0] for r in manifest(tmp_path).execute("select source_id from gone")] == [str(removed["id"])]
    assert snapshot(tmp_path) == remote_ids(site)


def test_an_add_and_a_delete_with_the_same_count_are_skipped_until_full(tmp_path):
    site = big_site(300)
    first_run(site, tmp_path)
    site.photos.pop(100)
    site.photos.insert(50, photo(7001))
    run_full_or_quick(site, tmp_path)
    assert listing_requests(site) == ["/album/albums", "/pic/list"]
    run_full_or_quick(site, tmp_path, full=True)
    assert fetched(site) == [7001]


def test_a_sub_album_with_non_monotonic_shot_times_diffs_the_same(tmp_path):
    ps = pics(*range(900))
    ps = ps[::3] + ps[1::3] + ps[2::3]
    for k, p in enumerate(ps):
        p["relate_time"] = f"2026-01-01 {(k * 7919) % 24:02d}:{(k * 31) % 60:02d}:00"
    site = FakePP([("A", ps)], photos=list(ps))
    first_run(site, tmp_path)
    site.albums[0][2][500:500] = pics(7001, 7002)
    site.photos[400:400] = pics(7001, 7002)
    assert run_full_or_quick(site, tmp_path) == {"done": 902}
    assert fetched(site) == [7001, 7002]
    one = [q["page"] for p, q in site.calls if p == "/album/one"]
    assert 2 <= len(one) <= 5
    assert snapshot(tmp_path, f"album:{site.albums[0][0]}") == [str(p["id"]) for p in site.albums[0][2]]


def test_groups_of_new_photos(tmp_path):
    site = FakePP([("A", pics(*range(250))), ("B", pics(300, 301))], photos=pics(*range(250), 300, 301, 400, 401))
    first_run(site, tmp_path)
    m = manifest(tmp_path)
    assert m.execute("select group_name from catalog where source_id=?", (sid(400),)).fetchone() == (None,)
    site.albums[0][2].append(photo(250))
    site.albums[1][2].append(photo(302))
    site.albums[1][2].append(photo(250))
    site.photos.extend(pics(250, 302, 500))
    run_full_or_quick(site, tmp_path)
    got = dict(m.execute("select source_id, group_name from catalog where source_id in (?,?,?)",
                         (sid(250), sid(302), sid(500))))
    assert got == {sid(250): "A", sid(302): "B", sid(500): None}


def test_a_known_photo_with_no_group_gets_the_sub_album_it_appears_in(tmp_path):
    site = FakePP([("A", pics(*range(250)))], photos=pics(*range(250), 400))
    first_run(site, tmp_path)
    m = manifest(tmp_path)
    site.albums[0][2].append(photo(400))
    m.execute("update catalog set group_name='Other' where source_id=?", (sid(5),))
    m.commit()
    site.albums[0][2].append(photo(5))
    run_full_or_quick(site, tmp_path)
    assert m.execute("select group_name from catalog where source_id=?", (sid(400),)).fetchone() == ("A",)
    assert m.execute("select group_name from catalog where source_id=?", (sid(5),)).fetchone() == ("Other",)
    assert not site.images


def test_a_moved_photo_makes_the_reconstruction_invalid_and_the_listing_is_re_read(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="download")
    site = big_site()
    first_run(site, tmp_path)
    moved = site.photos.pop(2000)
    site.photos.insert(10, moved)
    site.photos.insert(2500, photo(7001))
    assert run_full_or_quick(site, tmp_path) == {"done": 3001}
    assert fetched(site) == [7001]
    assert snapshot(tmp_path) == remote_ids(site)
    assert any("by linear read (inconsistent listing)" in r.getMessage() for r in caplog.records)
    assert len(list_pages_read(site)) >= 31
    assert manifest(tmp_path).execute("select count(*) from gone").fetchone()[0] == 0


def test_photos_uploaded_between_the_count_check_and_the_diff_are_still_listed(tmp_path):
    site = big_site(1000)
    first_run(site, tmp_path)
    site.photos.insert(500, photo(7001))
    real = site.__call__
    state = {"uploaded": False}

    def uploading(req):
        if req.url.path == "/pic/list" and req.url.params["page"] != "1" and not state["uploaded"]:
            state["uploaded"] = True
            site.photos.insert(100, photo(7002))
        return real(req)
    d = AlbumDownloader(client_for(uploading), adapter(site), tmp_path, sleep=lambda s: None)
    d.adapter.client = client_for(uploading)
    assert d.run() == {"done": 1002}
    d.close()
    assert fetched(site) == [7001, 7002]
    assert snapshot(tmp_path) == remote_ids(site)
    site.calls.clear()
    site.images.clear()
    assert run_full_or_quick(site, tmp_path) == {"done": 1002}
    assert not site.images
    site.calls.clear()
    run_full_or_quick(site, tmp_path)
    assert listing_requests(site) == ["/album/albums", "/pic/list"]


def test_expired_urls_on_a_diff_page_are_re_listed_and_the_bisect_continues(tmp_path):
    site = big_site()
    first_run(site, tmp_path)
    site.photos[1500:1500] = pics(7001, 7002)
    site.photos[2500:2500] = pics(7003)
    site.expire_next = 1
    assert run_full_or_quick(site, tmp_path) == {"done": 3003}
    assert fetched(site) == [7001, 7002, 7003]
    assert snapshot(tmp_path) == remote_ids(site)
    assert max(Counter(list_pages_read(site)).values()) >= 2


def test_a_relisted_diff_page_returns_the_same_rows_and_the_same_cursor(tmp_path):
    site = big_site()
    first_run(site, tmp_path)
    site.photos[1500:1500] = pics(7001, 7002)
    d = AlbumDownloader(client_for(site), adapter(site), tmp_path, sleep=lambda s: None)
    a = d.adapter
    a.attach(d.db, False)
    cursor = None
    while True:
        rows, nxt, _ = a.list_page(cursor)
        if rows:
            break
        cursor = nxt
    again = a.list_page(cursor)
    assert [r.source_id for r in again[0]] == [r.source_id for r in rows] and again[1] == nxt
    assert again[0][0].url != rows[0].url
    d.close()


def test_an_interrupted_diff_leaves_the_next_run_correct(tmp_path):
    site = FakePP([("A", pics(*range(250)))], photos=pics(*range(250)))
    first_run(site, tmp_path)
    site.albums[0][2].append(photo(7001))
    site.photos.extend([photo(7001)])
    site.photos[100:100] = pics(7002)
    real = site.__call__
    state = {"n": 0}

    def flaky(req):
        if req.url.path == "/pic/list":
            state["n"] += 1
            if state["n"] == 3:
                raise RuntimeError("network down")
        return real(req)
    d = AlbumDownloader(client_for(flaky), adapter(site), tmp_path, sleep=lambda s: None)
    d.adapter.client = client_for(flaky)
    with pytest.raises(RuntimeError):
        d.run()
    d.close()
    seen = Counter(site.images)
    site.calls.clear()
    assert run_full_or_quick(site, tmp_path) == {"done": 252}
    assert snapshot(tmp_path) == remote_ids(site)
    assert site.images[f"/plus/immediate/{ACTIVITY}/7001.jpg"] == 1
    assert site.images[f"/plus/immediate/{ACTIVITY}/7002.jpg"] == 1
    site.calls.clear()
    run_full_or_quick(site, tmp_path)
    assert listing_requests(site) == ["/album/albums", "/pic/list"]


def test_an_interrupted_sub_album_diff_is_seen_as_changed_again(tmp_path):
    site = FakePP([("A", pics(*range(250))), ("B", pics(300, 301))], photos=pics(*range(250), 300, 301))
    first_run(site, tmp_path)
    site.albums[0][2].append(photo(7001))
    site.albums[1][2].append(photo(7002))
    site.photos.extend(pics(7001, 7002))
    real = site.__call__

    def flaky(req):
        if req.url.path == "/album/one" and req.url.params["albumId"] == str(site.albums[1][0]):
            raise RuntimeError("network down")
        return real(req)
    d = AlbumDownloader(client_for(flaky), adapter(site), tmp_path, sleep=lambda s: None)
    d.adapter.client = client_for(flaky)
    with pytest.raises(RuntimeError):
        d.run()
    d.close()
    m = manifest(tmp_path)
    assert m.execute("select value from meta where key=?", (f"count:album:{site.albums[1][0]}",)).fetchone() == ("2",)
    assert m.execute("select value from meta where key=?", (f"count:album:{site.albums[0][0]}",)).fetchone() == ("251",)
    assert run_full_or_quick(site, tmp_path) == {"done": 254}
    assert snapshot(tmp_path, f"album:{site.albums[1][0]}") == [sid(300), sid(301), sid(7002)]
    assert snapshot(tmp_path) == remote_ids(site)


def drive(old, remote, size):
    gen = photoplus.bisect_listing(old, len(remote), size)
    reads = []
    try:
        page = next(gen)
        while True:
            reads.append(page)
            page = gen.send(remote[(page - 1) * size:page * size])
    except StopIteration as done:
        return done.value, reads


@pytest.mark.parametrize("seed", range(40))
def test_bisect_recovers_any_pure_add_or_pure_delete_exactly(seed):
    import random
    rng = random.Random(seed)
    size = rng.choice([3, 5, 10])
    old = [f"o{i}" for i in range(rng.randint(1, 120))]
    remote = list(old)
    if rng.random() < 0.5:
        for k in range(rng.randint(1, 12)):
            remote.insert(rng.randint(0, len(remote)), f"n{k}")
    else:
        for _ in range(rng.randint(1, min(12, len(remote)))):
            remote.pop(rng.randrange(len(remote)))
    (new, how), reads = drive(old, remote, size)
    assert new == remote
    assert len(reads) <= max(1, math.ceil(len(remote) / size)) + (math.ceil(len(remote) / size) + 1 if "linear" in how else 0)
    if how == "bisect":
        assert len(reads) == len(set(reads)) <= math.ceil(len(remote) / size)
