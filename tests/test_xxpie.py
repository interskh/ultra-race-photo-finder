import io
import json
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path

import httpx
import pytest
from PIL import Image

from photofinder import cli, config, db, races
from photofinder.index.stages import scan
from photofinder.sources import xxpie
from photofinder.sources.base import AlbumDownloader
from photofinder.sources.common import RetriesExhausted

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = json.loads((FIXTURES / "xxpie_list.json").read_text(encoding="utf-8"))
INFO = json.loads((FIXTURES / "xxpie_subalbum_info.json").read_text(encoding="utf-8"))
STYLE = json.loads((FIXTURES / "xxpie_style.json").read_text(encoding="utf-8"))
REGISTER = json.loads((FIXTURES / "xxpie_register.json").read_text(encoding="utf-8"))
ALBUM = "65178998a458227944415097"
URL = f"https://www.xxpie.com/m/album?id={ALBUM}"
KEY = f"xxpie-{ALBUM}"
TITLE = "八千个瞬间@2026崇礼168"
PHOTOS = LISTING["result"]["photos"]
buf = io.BytesIO()
Image.effect_noise((200, 150), 64).convert("RGB").save(buf, "JPEG")
PREVIEW = buf.getvalue()


def synthetic(n):
    return [dict(PHOTOS[0], album_ossobject_id=str(900000000000000000 + i), file_name=f"ZZBB{i:04d}.jpg",
                 url_large1920=f"https://imagex.xxpie.com/arrb--159159--{i}--4?sign=s{i}")
            for i in range(n)]


class FakeXx:
    def __init__(self, photos=None):
        self.photos = PHOTOS if photos is None else photos
        self.calls = []
        self.images = Counter()
        self.registrations = []
        self.bad_codes = 0
        self.register_code = 0
        self.register_status = 200
        self.photo_count = None
        self._lock = threading.Lock()

    def __call__(self, req):
        if req.url.host == "imagex.xxpie.com":
            with self._lock:
                self.images[req.url.path] += 1
            return httpx.Response(200, content=PREVIEW)
        path = req.url.path.rsplit("/", 1)[1]
        if path == "registerVisitorUser":
            self.registrations.append(json.loads(req.content))
            if self.register_status != 200:
                return httpx.Response(self.register_status)
            if self.register_code:
                return httpx.Response(200, json={"code": self.register_code, "message": "nope", "result": None})
            return httpx.Response(200, json={**REGISTER, "result": {**REGISTER["result"],
                                                                     "token": f"tok{len(self.registrations)}"}})
        q = dict(req.url.params)
        self.calls.append((path, q, req.headers.get("x-access-token")))
        if self.bad_codes:
            self.bad_codes -= 1
            return httpx.Response(200, json={"code": 401, "message": "token expired", "result": None})
        if path == "queryAlbumStyleH5":
            return httpx.Response(200, json=STYLE)
        if path == "querySubAlbumPhotoInfo":
            count = len(self.photos) if self.photo_count is None else self.photo_count
            result = {k: v for k, v in INFO["result"].items() if k != "photo_count"}
            return httpx.Response(200, json={**INFO, "result": result if count == "absent" else
                                             {**result, "photo_count": count}})
        page, size = int(q["page_no"]), int(q["page_size"])
        return httpx.Response(200, json={"code": 0, "result": {
            "count": 0, "photos": self.photos[(page - 1) * size:page * size], "layout": None}})

    def lists(self):
        return [(q, tok) for p, q, tok in self.calls if p == "queryAlbumItemsPgByDefaultSort"]


def client_for(site):
    return httpx.Client(headers=xxpie.HEADERS, transport=httpx.MockTransport(site))


def adapter(site, **kw):
    return xxpie.Adapter(client_for(site), ALBUM, sleep=lambda s: None, **kw)


def walk(a):
    cursors, cursor = [], None
    while True:
        rows, cursor, total = a.list_page(cursor)
        cursors.append(cursor)
        if cursor is None:
            return cursors, total


def test_meta_registers_a_visitor_and_reads_title_and_photo_count():
    site = FakeXx()
    site.photo_count = 3670
    a = adapter(site)
    assert a.meta() == {"title": TITLE, "total": 3670}
    (reg,) = site.registrations
    assert reg["platform"] == "H5" and re.fullmatch(r"[0-9a-f]{32}", reg["username"])
    assert [(p, q, tok) for p, q, tok in site.calls] == [
        ("queryAlbumStyleH5", {"album_id": ALBUM, "is_visited": "0", "source": "H5", "platform": "H5"}, "tok1"),
        ("querySubAlbumPhotoInfo", {"album_id": ALBUM, "platform": "H5"}, "tok1")]


def test_real_subalbum_info_count_is_the_total():
    a = adapter(FakeXx())
    assert a.ok(INFO)["photo_count"] == 3670


@pytest.mark.parametrize("count", ["absent", "3670", True])
def test_unusable_photo_count_leaves_total_unknown(count):
    site = FakeXx()
    site.photo_count = count
    a = adapter(site)
    assert a.meta()["total"] is None
    assert a.list_page(None)[2] is None


def test_listing_sends_the_watermarked_default_query():
    site = FakeXx()
    a = adapter(site)
    a.list_page(None)
    (q, tok), = site.lists()
    assert q == {"album_id": ALBUM, "page_no": "1", "page_size": "60", "sub_album_id": "ALL", "no_watermark": "",
                 "platform": "H5"}
    assert tok == "tok1"


def test_pages_step_by_one_until_a_short_page():
    site = FakeXx(synthetic(130))
    a = adapter(site)
    a.meta()
    assert walk(a) == ([2, 3, None], 130)
    assert [q["page_no"] for q, _ in site.lists()] == ["1", "2", "3"]
    assert len(site.registrations) == 1


def test_full_last_page_needs_one_more_empty_page():
    site = FakeXx(synthetic(60))
    a = adapter(site)
    assert walk(a)[0] == [2, None]


def test_nonzero_code_renews_the_token_and_retries():
    site = FakeXx()
    a = adapter(site)
    a.list_page(None)
    site.bad_codes = 2
    rows, _, _ = a.list_page(None)
    assert len(rows) == 3
    assert [tok for _, tok in site.lists()] == ["tok1", "tok1", "tok2", "tok3"]
    assert len(site.registrations) == 3
    assert len({r["username"] for r in site.registrations}) == 3


def test_persistent_nonzero_code_gives_up_without_an_extra_registration():
    site = FakeXx()
    site.bad_codes = 99
    a = adapter(site, tries=3)
    with pytest.raises(RetriesExhausted):
        a.list_page(None)
    assert [tok for _, tok in site.lists()] == ["tok1", "tok2", "tok3"]
    assert len(site.registrations) == 3


def test_failed_registration_is_retried_then_gives_up():
    site = FakeXx()
    site.register_code = 500
    a = adapter(site, tries=2)
    with pytest.raises(RetriesExhausted):
        a.list_page(None)
    assert len(site.registrations) == 2 and site.calls == []


def test_rows_map_from_the_real_listing():
    a = adapter(FakeXx())
    rows, cursor, _ = a.list_page(None)
    assert cursor is None
    r = rows[0]
    assert (r.source_id, r.fname, r.photographer_uid, r.photographer, r.group_name, r.taken_at, r.width, r.height) == (
        "413294397729276278", "FSYS2092.jpg", "000000000000000000000a01", "Photographer E", None, "2026-07-11 11:44:26",
        2667, 4000)
    assert a.preview_url(r) == PHOTOS[0]["url_large1920"]
    assert [x.taken_at for x in rows] == ["2026-07-11 11:44:26", "2026-07-11 11:43:53", "2026-07-11 09:29:32"]


def test_photographer_uid_matches_the_subalbum_uploaders():
    uploaders = {u["sys_user_id"]: u["nick_name"] for u in INFO["result"]["upload_bys"]}
    for p in PHOTOS:
        r = xxpie.to_row(p)
        assert uploaders[r.photographer_uid] == r.photographer


def test_utc_record_time_becomes_shanghai_time_across_midnight():
    assert xxpie.shot_time("2026-07-10T16:30:00.000Z") == "2026-07-11 00:30:00"
    assert xxpie.shot_time("2026-07-10T16:30:00Z") == "2026-07-11 00:30:00"


@pytest.mark.parametrize("value", ["2026-07-11 03:44:26", "2026-07-11T03:44:26.084", "2026-07-11T03:44:26+08:00",
                                   "2026-13-11T03:44:26.084Z", "", None, 1783741466084, "2026-07-11T03:44:26.084Z\n"])
def test_unusable_record_time_is_none(value):
    assert xxpie.shot_time(value) is None


def test_row_without_photographer_has_no_uid():
    r = xxpie.to_row(dict(PHOTOS[0], photographer=None))
    assert (r.photographer_uid, r.photographer) == (None, None)


def test_unsafe_or_missing_ids_are_skipped_and_leave_the_total_honest():
    no_id = {k: v for k, v in PHOTOS[2].items() if k != "album_ossobject_id"}
    site = FakeXx([dict(PHOTOS[0], album_ossobject_id="../evil"), no_id, PHOTOS[1]])
    a = adapter(site)
    a.meta()
    rows, _, total = a.list_page(None)
    assert [r.source_id for r in rows] == ["955214179341058648"]
    assert total == 1
    assert a.list_page(None)[2] == 1


def test_downloader_fetches_every_page_and_a_rerun_fetches_nothing(tmp_path):
    site = FakeXx(synthetic(130))
    for _ in range(2):
        d = AlbumDownloader(client_for(site), adapter(site), tmp_path, sleep=lambda s: None)
        try:
            assert d.run() == {"done": 130}
        finally:
            d.close()
    assert sum(site.images.values()) == 130 and set(site.images.values()) == {1}
    m = sqlite3.connect(tmp_path / "manifest.sqlite")
    assert dict(m.execute("select key, value from meta")) == {"title": TITLE}
    assert m.execute("select taken_at from catalog where source_id = '900000000000000005'").fetchone() == (
        "2026-07-11 11:44:26",)


def test_downloader_reports_photos_missing_from_the_listing(tmp_path):
    site = FakeXx(synthetic(70))
    site.photo_count = 75
    d = AlbumDownloader(client_for(site), adapter(site), tmp_path, sleep=lambda s: None)
    try:
        assert d.run() == {"done": 70, "missing": 5}
    finally:
        d.close()


def test_skipped_rows_leave_no_missing_gap(tmp_path):
    site = FakeXx([dict(PHOTOS[0], album_ossobject_id="../evil"), *PHOTOS[1:]])
    d = AlbumDownloader(client_for(site), adapter(site), tmp_path, sleep=lambda s: None)
    try:
        assert d.run() == {"done": 2}
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
    out = races.album_dir("2026-x", KEY)
    d = AlbumDownloader(client_for(FakeXx()), adapter(FakeXx()), out, sleep=lambda s: None)
    try:
        assert d.run() == {"done": 3}
    finally:
        d.close()
    r = races.race_dir("2026-x")
    conn = db.connect(r)
    assert scan(conn, r)["new"] == 3
    got = conn.execute("select relpath, taken_at, album, album_key, photographer_uid, photographer, grp from photos "
                       "where source_photo_id = '413294397729276278'").fetchone()
    assert tuple(got) == (f"albums/{KEY}/photos/413294397729276278.jpg", "2026-07-11 11:44:26", TITLE, KEY,
                          "xxpie:000000000000000000000a01", "Photographer E", None)


@pytest.fixture
def site(data_root, monkeypatch):
    site = FakeXx()
    monkeypatch.setattr(cli, "album_client", lambda platform: client_for(site))
    races.add_race("2026-x", "X")
    return site


def registry(root):
    return json.loads((root / "races.json").read_text(encoding="utf-8"))


def test_album_add_fetches_the_title_from_the_users_url(data_root, site, capsys):
    cli.main(["album", "add", "2026-x", URL])
    assert registry(data_root)["races"][0]["albums"] == [
        {"key": KEY, "platform": "xxpie", "site_id": ALBUM, "url": URL, "title": TITLE}]
    assert [p for p, _, _ in site.calls] == ["queryAlbumStyleH5", "querySubAlbumPhotoInfo"]
    assert KEY in capsys.readouterr().out


def test_album_add_title_fetch_failure_suggests_title(data_root, site):
    site.register_status = 403
    with pytest.raises(SystemExit) as e:
        cli.main(["album", "add", "2026-x", URL])
    assert "--title" in str(e.value.code)
    assert registry(data_root)["races"][0]["albums"] == []


def test_cli_download_runs_the_xxpie_album(data_root, site, monkeypatch):
    monkeypatch.setattr(cli, "album_downloader",
                        lambda client, adapter, out_dir: AlbumDownloader(client, adapter, out_dir, sleep=lambda s: None))
    races.add_album("2026-x", URL, TITLE)
    cli.main(["download", "2026-x"])
    photos = races.album_dir("2026-x", KEY) / "photos"
    assert sorted(p.name for p in photos.iterdir()) == sorted(f"{p['album_ossobject_id']}.jpg" for p in PHOTOS)
