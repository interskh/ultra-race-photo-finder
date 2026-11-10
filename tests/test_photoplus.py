import hashlib
import io
import itertools
import json
import logging
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
        self.images = Counter()
        self._lock = threading.Lock()

    def signed(self, ps):
        self.listings += 1
        return [dict(p, big_img=f"{p['big_img']}?sign=L{self.listings}") for p in ps]

    def __call__(self, req):
        if req.url.host == "pb.plusx.cn":
            with self._lock:
                self.images[req.url.path] += 1
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


def test_a_changed_sub_album_count_lists_everything_and_refreshes_the_snapshot(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="download")
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    site.calls.clear()
    site.albums[1][2].append(photo(302))
    site.photos.append(photo(302))
    assert run_full_or_quick(site, tmp_path) == {"done": 254}
    assert len(listing_requests(site)) > 5
    assert f"photoplus: album:{site.albums[1][0]} changed 2 -> 3" in [r.getMessage() for r in caplog.records]
    assert "photoplus: list changed 253 -> 254" in [r.getMessage() for r in caplog.records]
    site.calls.clear()
    run_full_or_quick(site, tmp_path)
    assert listing_requests(site) == ["/album/albums", "/pic/list"]


def test_a_changed_list_count_alone_lists_everything(tmp_path):
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    site.calls.clear()
    site.photos.append(photo(500))
    assert run_full_or_quick(site, tmp_path) == {"done": 254}
    assert len(listing_requests(site)) > 5


def test_a_new_sub_album_lists_everything(tmp_path):
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    site.calls.clear()
    site.albums.append((site.albums[-1][0] + 1, "C", []))
    assert run_full_or_quick(site, tmp_path) == {"done": 253}
    assert ("/album/one" in listing_requests(site)) and len(listing_requests(site)) > 5
    m = manifest(tmp_path)
    assert len(json.loads(m.execute("select value from meta where key='listing:albums'").fetchone()[0])) == 3


def test_a_removed_sub_album_lists_everything_and_drops_its_snapshot(tmp_path):
    site = quick_site()
    run_full_or_quick(site, tmp_path)
    gone = f"album:{site.albums[1][0]}"
    site.calls.clear()
    del site.albums[1]
    run_full_or_quick(site, tmp_path)
    assert len(listing_requests(site)) > 3
    m = manifest(tmp_path)
    assert m.execute("select count(*) from listing where list_key=?", (gone,)).fetchone()[0] == 0
    assert m.execute("select count(*) from meta where key=?", (f"count:{gone}",)).fetchone()[0] == 0


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
        d.run()
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
