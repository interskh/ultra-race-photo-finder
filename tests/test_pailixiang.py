import io
import itertools
import json
import random
import sqlite3
import threading
from collections import Counter
from pathlib import Path

import httpx
import pytest
from PIL import Image

from photofinder import cli, config, db, races
from photofinder.index.stages import scan
from photofinder.sources import pailixiang
from photofinder.sources.base import AlbumDownloader
from photofinder.sources.common import RetriesExhausted

FIXTURES = Path(__file__).parent / "fixtures"
VIEW = json.loads((FIXTURES / "pailixiang_view.json").read_text(encoding="utf-8"))
LISTING = json.loads((FIXTURES / "pailixiang_list.json").read_text(encoding="utf-8"))
URL = "https://live.pailixiang.com/album/a13800138000"
KEY = "pailixiang-a13800138000"  # album key, not a secret; gitleaks:allow
TITLE = "2026FUGA贡嘎100冰川极境赛"
ALBUM_ID = "26456848041475797975"
buf = io.BytesIO()
Image.effect_noise((200, 150), 64).convert("RGB").save(buf, "JPEG")
PREVIEW = buf.getvalue()


def synthetic(n):
    base = LISTING["Data"][0]
    return [dict(base, ID=f"{i:032x}", Name=f"LUY{i:05d}.jpg", FileName=f"{1000 + i}.jpg",
                 BigImageUrl=f"https://img.pailixiang.com/album/a13800138000/{1000 + i}.jpg?x-oss-process=style/pbig")
            for i in range(n)]


class FakePlx:
    def __init__(self, photos=None):
        self.photos = LISTING["Data"] if photos is None else photos
        self.calls = []
        self.images = Counter()
        self.lists = 0
        self.code = 0
        self.view_status = 200
        self.total_for = lambda listing: len(self.photos)
        self._lock = threading.Lock()

    def __call__(self, req):
        if req.url.host == "img.pailixiang.com":
            with self._lock:
                self.images[req.url.path] += 1
            return httpx.Response(200, content=PREVIEW)
        body = json.loads(req.content)
        action = req.url.path.rsplit("/", 1)[1]
        self.calls.append((action, body))
        self.headers = {k: req.headers.get(k) for k in ("content-type", "referer", "origin")}
        if action == "AlbumGetView":
            return httpx.Response(self.view_status, json=VIEW)
        self.lists += 1
        if self.code:
            return httpx.Response(200, json={"Code": self.code, "Msg": "illegal request", "Data": None})
        start = body["StartIndex"]
        return httpx.Response(200, json={"Code": 0, "Msg": "", "Data": self.photos[start - 1:start - 1 + body["SearchCount"]],
                                         "TotalCount": self.total_for(self.lists), "OptTime": f"2026-09-29 08:14:{self.lists:02d}"})

    def lists_sent(self):
        return [b for a, b in self.calls if a == "AlbumSearchPhoto"]


def client_for(site):
    return httpx.Client(headers=pailixiang.HEADERS, transport=httpx.MockTransport(site))


def adapter(site, **kw):
    return pailixiang.Adapter(client_for(site), "a13800138000", sleep=lambda s: None, **kw)


def walk(a):
    cursors, cursor = [], None
    while True:
        rows, cursor, total = a.list_page(cursor)
        cursors.append(cursor)
        if cursor is None:
            return cursors, total


def test_ak_is_the_prefixed_key_with_three_copied_digits():
    random.seed(7)
    for _ in range(50):
        value = pailixiang.ak()
        assert len(value) == 35 and value[:3].isdigit()
        key = list("REMOVED-pailixiang-web-client-key")
        for d in value[:3]:
            key[int(d) + 15] = key[int(d)]
        assert value[3:] == "".join(key)
    random.seed(7)
    assert len({pailixiang.ak() for _ in range(50)}) > 1


def test_meta_reads_title_and_album_id_from_the_real_view():
    site = FakePlx()
    a = adapter(site)
    assert a.meta() == {"title": TITLE, "total": None}
    assert a.meta_items() == {"album_id": ALBUM_ID}
    (action, body), = site.calls
    assert action == "AlbumGetView"
    assert {k: body[k] for k in ("ID", "AccessType", "ClientType", "tt", "ct", "cv", "lang", "pid")} == {
        "ID": "13800138000", "AccessType": "1", "ClientType": 0, "tt": "", "ct": 0, "cv": "169", "lang": "cn",
        "pid": "albumview"}
    assert len(body["ak"]) == 35
    assert site.headers == {"content-type": "application/json;charset=UTF-8", "referer": "https://live.pailixiang.com/",
                            "origin": "https://live.pailixiang.com"}


def test_pages_step_by_80_and_echo_the_first_opt_time():
    site = FakePlx(synthetic(170))
    a = adapter(site)
    a.meta()
    assert walk(a) == ([81, 161, None], 170)
    a.list_page(None)
    sent = site.lists_sent()
    assert [b["StartIndex"] for b in sent] == [1, 81, 161, 1]
    assert [b["OptTime"] for b in sent] == ["", "2026-09-29 08:14:01", "2026-09-29 08:14:01", "2026-09-29 08:14:01"]
    assert all(b["AlbumID"] == ALBUM_ID and b["SearchCount"] == 80 and b["pid"] == "albumview" for b in sent)
    assert len({b["ak"] for b in sent} | {site.calls[0][1]["ak"]}) > 1


def test_first_positive_total_count_is_kept():
    site = FakePlx(synthetic(170))
    site.total_for = lambda listing: {1: 0, 2: 200}.get(listing, 0)
    a = adapter(site)
    a.meta()
    assert [a.list_page(c)[2] for c in (None, 81, 161)] == [None, 200, 200]


def test_later_zero_total_count_still_reports_missing(tmp_path):
    site = FakePlx(synthetic(170))
    site.total_for = lambda listing: 200 if listing == 1 else 0
    d = AlbumDownloader(client_for(site), adapter(site), tmp_path, sleep=lambda s: None)
    try:
        assert d.run() == {"done": 170, "missing": 30}
    finally:
        d.close()


def test_full_last_page_needs_one_more_empty_page():
    site = FakePlx(synthetic(80))
    a = adapter(site)
    a.meta()
    assert walk(a) == ([81, None], 80)


def test_nonzero_code_is_retried_with_a_fresh_ak_then_gives_up(monkeypatch):
    counter = itertools.count()
    monkeypatch.setattr(pailixiang, "ak", lambda: f"ak{next(counter)}")
    site = FakePlx()
    site.code = 8
    a = adapter(site, tries=3)
    a.album_id = ALBUM_ID
    with pytest.raises(RetriesExhausted):
        a.list_page(None)
    assert [b["ak"] for b in site.lists_sent()] == ["ak0", "ak1", "ak2"]


def test_rows_map_from_the_real_listing():
    site = FakePlx()
    a = adapter(site)
    a.meta()
    rows, cursor, total = a.list_page(None)
    assert (cursor, total) == (None, 3)
    r = rows[0]
    assert (r.source_id, r.fname, r.photographer_uid, r.photographer, r.group_name, r.taken_at, r.width, r.height) == (
        "49771c0301d22e325f54906509143214", "OCN04371.jpg", "00000000-0000-4000-8000-000000000001", "Photographer A", None,
        "2026-09-26 06:21:34", 8252, 5501)
    assert a.preview_url(r) == LISTING["Data"][0]["BigImageUrl"]
    assert [x.fname for x in rows] == ["OCN04371.jpg", "SJQ37447.jpg", "UTM35027.jpg"]


@pytest.mark.parametrize("value", ["2026/09/26 06:21:34", "2026-09-26T06:21:34", "2026-09-26 06:21", "", None,
                                   "2026-09-26 06:21:34\n"])
def test_unusable_shoot_time_is_none(value):
    assert pailixiang.to_row(dict(LISTING["Data"][0], ShootTime=value)).taken_at is None


def test_unsafe_ids_are_skipped():
    site = FakePlx([dict(LISTING["Data"][0], ID="../evil"), LISTING["Data"][1]])
    a = adapter(site)
    a.meta()
    rows, _, _ = a.list_page(None)
    assert [r.source_id for r in rows] == ["6c048314839630301264053265e57922"]


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
    site = FakePlx()
    for _ in range(2):
        d = AlbumDownloader(client_for(site), adapter(site), out, sleep=lambda s: None)
        try:
            assert d.run() == {"done": 3}
        finally:
            d.close()
    assert sum(site.images.values()) == 3
    m = sqlite3.connect(out / "manifest.sqlite")
    assert dict(m.execute("select key, value from meta")) == {"title": TITLE, "album_id": ALBUM_ID}
    assert m.execute("select count(*) from catalog where status='done'").fetchone() == (3,)
    m.close()
    r = races.race_dir("2026-x")
    conn = db.connect(r)
    assert scan(conn, r)["new"] == 3
    got = conn.execute("select relpath, taken_at, album, album_key, photographer_uid, photographer, grp from photos "
                       "where source_photo_id = '49771c0301d22e325f54906509143214'").fetchone()
    assert tuple(got) == (f"albums/{KEY}/photos/49771c0301d22e325f54906509143214.jpg", "2026-09-26 06:21:34", TITLE,
                          KEY, "pailixiang:00000000-0000-4000-8000-000000000001", "Photographer A", None)


@pytest.fixture
def site(data_root, monkeypatch):
    site = FakePlx()
    monkeypatch.setattr(cli, "album_client", lambda platform: client_for(site))
    races.add_race("2026-x", "X")
    return site


def registry(root):
    return json.loads((root / "races.json").read_text(encoding="utf-8"))


def album_add(*argv):
    cli.main(["album", "add", *argv])


def refused(*argv):
    with pytest.raises(SystemExit) as e:
        album_add(*argv)
    assert e.value.code not in (0, None)
    return str(e.value.code)


def test_album_add_fetches_the_title(data_root, site, capsys):
    album_add("2026-x", URL)
    albums = registry(data_root)["races"][0]["albums"]
    assert albums == [{"key": KEY, "platform": "pailixiang", "site_id": "a13800138000", "url": URL, "title": TITLE}]
    assert [a for a, _ in site.calls] == ["AlbumGetView"]
    out = capsys.readouterr().out
    assert KEY in out and TITLE in out and "scripts/download.sh 2026-x" in out and "photofinder index 2026-x" in out


def test_album_add_with_title_skips_the_network(data_root, site):
    album_add("2026-x", URL, "--title", "Glacier")
    assert registry(data_root)["races"][0]["albums"][0]["title"] == "Glacier"
    assert site.calls == []


def test_album_add_yipai_stores_no_title(data_root, site):
    album_add("2026-x", "https://www.yipai360.com/photolivepc/?orderId=1001")
    assert registry(data_root)["races"][0]["albums"][0]["title"] is None
    assert site.calls == []


@pytest.mark.parametrize("argv, message", [
    (("2026-typo", URL), "no race '2026-typo'"),
    (("2026-x", "https://live.pailixiang.com/grapher/u123"), "cannot find the pailixiang album id"),
    (("2026-x", "https://example.com/album/a1"), "unsupported album URL"),
])
def test_album_add_refuses_before_any_request(data_root, site, argv, message):
    before = registry(data_root)
    assert message in refused(*argv)
    assert site.calls == [] and registry(data_root) == before


def test_album_add_refuses_a_duplicate_before_any_request(data_root, site):
    races.add_race("2026-y", "Y")
    album_add("2026-x", URL, "--title", "Glacier")
    before = registry(data_root)
    assert "already belongs to race 2026-x" in refused("2026-y", URL)
    assert site.calls == [] and registry(data_root) == before


def test_album_add_title_fetch_failure_suggests_title(data_root, site):
    site.view_status = 403
    assert "--title" in refused("2026-x", URL)
    assert registry(data_root)["races"][0]["albums"] == []
