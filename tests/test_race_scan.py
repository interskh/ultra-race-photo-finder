import logging
import sqlite3
from datetime import datetime


from photofinder import db, originals, races, search
from photofinder.index.stages import scan, to_blob
from photofinder.sources import yipai
from photofinder.web import app as web
from test_scan import camera_exif, jpeg, rows
from test_search import A, X
from test_web import RaceClient, ME, no_real_models  # noqa: F401

CATALOG = """create table catalog(source_id text primary key, file text, fname text, photographer_uid text,
  photographer text, group_name text, taken_at text, width integer, height integer,
  status text not null default 'pending', error text)"""


def yipai_album(folder, photos, tags, photographers, view=True):
    folder.mkdir(parents=True, exist_ok=True)
    m = sqlite3.connect(folder / "manifest.sqlite")
    m.executescript(yipai.SCHEMA)
    if not view:
        m.execute("drop view catalog")
    m.executemany("insert into photos(photo_id, order_id, tag_id, uid, fname) values (?,?,?,?,?)", photos)
    m.executemany("insert into tags(tag_id, name) values (?,?)", tags)
    m.executemany("insert into photographers(uid, nickname) values (?,?)", photographers)
    m.commit()
    m.close()


def make_race():
    races.add_race("2026-x", "2026 X")
    races.add_album("2026-x", "https://www.yipai360.com/photolivepc/?orderId=1001", "Yipai One")
    races.add_album("2026-x", "https://www.yipai360.com/photolivepc/?orderId=1002", "Yipai Two")
    r = races.race_dir("2026-x")
    a1, a2, xx = (r / "albums" / k for k in ("yipai-1001", "yipai-1002", "xxpie-abc"))
    jpeg(a1 / "photos" / "101.jpg", camera_exif())
    jpeg(a1 / "photos" / "102.jpg")
    yipai_album(a1, [(101, "1001", 7, "123", "IMG_101.JPG"), (102, "1001", 8, "u2", "IMG_102.JPG")],
                [(7, "终点"), (8, "起点")], [("123", "cam-a"), ("u2", "cam-b")])
    jpeg(a2 / "photos" / "101.jpg")
    yipai_album(a2, [(101, "1002", 9, "123", "DSC_101.JPG")], [(9, "山顶")], [("123", "cam-z")], view=False)
    jpeg(xx / "photos" / "abc.jpg")
    jpeg(xx / "photos" / "late.jpg", camera_exif(taken="2026:09:25 12:00:00"))
    jpeg(xx / "photos" / "bad.jpg")
    m = sqlite3.connect(xx / "manifest.sqlite")
    m.execute(CATALOG)
    m.executemany("insert into catalog(source_id, fname, photographer_uid, photographer, group_name, taken_at) "
                  "values (?,?,?,?,?,?)", [("abc", "X1.JPG", "s77", "xx-cam", None, "2026-09-25 08:30:00"),
                                           ("late", "X2.JPG", "s77", "xx-cam", "A组", "2026-09-25 08:40:00"),
                                           ("bad", "X3.JPG", None, None, None, "25/09/2026")])
    m.commit()
    m.close()
    jpeg(r / "stray.jpg")
    jpeg(r / "albums" / "loose.jpg")
    return r


def test_race_scan_reads_each_album_catalog(tmp_path):
    r = make_race()
    conn = db.connect(r)
    assert scan(conn, r) == {"new": 6, "existing": 0, "errors": 0, "skipped": 2}
    got = {k: (v["source_photo_id"], v["album_key"], v["album"], v["grp"], v["photographer_uid"], v["photographer"])
           for k, v in rows(conn).items()}
    assert got == {
        "albums/yipai-1001/photos/101.jpg": ("101", "yipai-1001", "Yipai One", "终点", "yipai:123", "cam-a"),
        "albums/yipai-1001/photos/102.jpg": ("102", "yipai-1001", "Yipai One", "起点", "yipai:u2", "cam-b"),
        "albums/yipai-1002/photos/101.jpg": ("101", "yipai-1002", "Yipai Two", "山顶", "yipai:123", "cam-z"),
        "albums/xxpie-abc/photos/abc.jpg": ("abc", "xxpie-abc", "xxpie-abc", None, "xxpie:s77", "xx-cam"),
        "albums/xxpie-abc/photos/late.jpg": ("late", "xxpie-abc", "xxpie-abc", "A组", "xxpie:s77", "xx-cam"),
        "albums/xxpie-abc/photos/bad.jpg": ("bad", "xxpie-abc", "xxpie-abc", None, None, None),
    }
    assert scan(conn, r) == {"new": 0, "existing": 6, "errors": 0, "skipped": 2}


def test_stray_race_images_counted_and_warned_once_per_scan(tmp_path, caplog):
    r = make_race()
    conn = db.connect(r)
    for _ in range(2):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="index"):
            assert scan(conn, r)["skipped"] == 2
        stray = [m for m in (x.getMessage() for x in caplog.records) if "skipping" in m]
        assert stray == ["skipping 2 images not under albums/<album>/ in a race directory, e.g. albums/loose.jpg"]
    assert not {"stray.jpg", "albums/loose.jpg"} & set(rows(conn))


def test_symlinked_album_dir_warns_and_is_not_scanned(tmp_path, caplog):
    r = make_race()
    real = tmp_path / "elsewhere" / "yipai-1003"
    jpeg(real / "photos" / "5.jpg")
    (r / "albums" / "yipai-1003").symlink_to(real, target_is_directory=True)
    conn = db.connect(r)
    with caplog.at_level(logging.WARNING, logger="index"):
        assert scan(conn, r)["new"] == 6
    assert [x.getMessage() for x in caplog.records if "symlink" in x.getMessage()] == [
        "album yipai-1003 is a symlinked directory and will not be scanned; "
        "use a real directory (file symlinks inside it are fine)"]


def test_catalog_time_fills_missing_exif_and_time_filter_matches(tmp_path):
    r = make_race()
    conn = db.connect(r)
    scan(conn, r)
    got = rows(conn)
    times = {k.rsplit("/", 1)[1]: (v["taken_at"], v["taken_ts"]) for k, v in got.items() if "xxpie" in k}
    assert times == {"abc.jpg": ("2026-09-25 08:30:00", datetime(2026, 9, 25, 8, 30).timestamp()),
                     "late.jpg": ("2026-09-25 12:00:00", datetime(2026, 9, 25, 12).timestamp()),
                     "bad.jpg": (None, None)}
    assert got["albums/yipai-1001/photos/101.jpg"]["taken_at"] == "2026-09-26 07:15:00"
    where, args = search.filter_where(search.Filters(start="2026-09-25 08:00:00", end="2026-09-25 09:00:00"))
    assert [p for (p,) in conn.execute(f"select relpath from photos ph where {' and '.join(where)}", args)] == \
        ["albums/xxpie-abc/photos/abc.jpg"]


def embed_all(conn):
    for (photo_id,) in conn.execute("select id from photos where status = 'ok' order by id").fetchall():
        pid = conn.execute("insert into persons(photo_id, x1, y1, x2, y2, conf) values (?, 0, 0, 20, 30, 0.9)",
                           (photo_id,)).lastrowid
        conn.execute("insert into emb_person_osnet values (?,?)", (pid, to_blob(A)))
        conn.execute("insert into emb_person_siglip values (?,?)", (pid, to_blob(X)))
    conn.commit()


def test_race_facets_list_albums_and_album_filter_restricts(tmp_path):
    r = make_race()
    conn = db.connect(r)
    scan(conn, r)
    embed_all(conn)
    first = conn.execute("select min(id) from persons").fetchone()[0]
    api = RaceClient(web.create_app(r))
    facets = api.get("/api/facets").json()
    assert {a["name"]: a["photos"] for a in facets["albums"]} == {"Yipai One": 2, "Yipai Two": 1, "xxpie-abc": 3}
    assert {g["name"] for g in facets["groups"]} == {"终点", "起点", "山顶", "A组"}
    assert {(p["name"], p["uid"]) for p in facets["photographers"]} == {
        ("cam-a", "yipai:123"), ("cam-b", "yipai:u2"), ("cam-z", "yipai:123"), ("xx-cam", "xxpie:s77")}
    assert facets["originals"] is True
    res = api.post("/api/search", json={"profile_id": ME, "persons": [first], "albums": ["Yipai Two"]})
    assert [x["relpath"] for x in res.json()["results"]] == ["albums/yipai-1002/photos/101.jpg"]
    res = api.post("/api/search", json={"profile_id": ME, "persons": [first], "photographers": ["123"]})
    assert sorted(x["relpath"] for x in res.json()["results"]) == \
        ["albums/yipai-1001/photos/101.jpg", "albums/yipai-1002/photos/101.jpg"]


def test_rows_of_reads_order_and_fname_from_each_album_manifest(tmp_path):
    r = make_race()
    conn = db.connect(r)
    scan(conn, r)
    meta = web.photo_meta(conn, [i for (i,) in conn.execute("select id from photos")])
    got = {m["relpath"]: (m["platform"], m["order_id"], m["fname"], m["site"]["url"])
           for m in originals.rows_of(r, list(meta.values()))}
    yp, xx = "https://www.yipai360.com/photolivepc/?orderId=", "https://www.xxpie.com/m/albumFilenameSearch?album_id=abc"
    assert got == {
        "albums/yipai-1001/photos/101.jpg": ("yipai", "1001", "IMG_101.JPG", yp + "1001"),
        "albums/yipai-1001/photos/102.jpg": ("yipai", "1001", "IMG_102.JPG", yp + "1001"),
        "albums/yipai-1002/photos/101.jpg": ("yipai", "1002", "DSC_101.JPG", yp + "1002"),
        "albums/xxpie-abc/photos/abc.jpg": ("xxpie", None, "X1.JPG", xx + "&search_word=X1.JPG"),
        "albums/xxpie-abc/photos/late.jpg": ("xxpie", None, "X2.JPG", xx + "&search_word=X2.JPG"),
        "albums/xxpie-abc/photos/bad.jpg": ("xxpie", None, "X3.JPG", xx + "&search_word=X3.JPG"),
    }


def test_has_originals_for_race_needs_an_album_of_a_downloadable_platform(tmp_path):
    r = tmp_path / "race"
    (r / "albums" / "elsewhere-abc").mkdir(parents=True)
    (r / "albums" / "elsewhere-abc" / "manifest.sqlite").write_bytes(b"")
    assert not originals.has_originals(r)
    for platform in ("pailixiang", "xxpie"):
        (r / "albums" / f"{platform}-abc").mkdir(parents=True)
        (r / "albums" / f"{platform}-abc" / "manifest.sqlite").write_bytes(b"")
        assert originals.has_originals(r)
        (r / "albums" / f"{platform}-abc" / "manifest.sqlite").unlink()
    assert not originals.has_originals(r)
    yipai_album(r / "albums" / "yipai-1001", [], [], [])
    assert originals.has_originals(r)
