import fcntl
import sqlite3
import subprocess
import sys
from datetime import datetime

import numpy as np
import pytest
from PIL import Image

from photofinder import cli, db
from photofinder.index import stages
from photofinder.index.stages import scan
from photofinder.sources.yipai import SCHEMA as MANIFEST_SCHEMA


def jpeg(path, exif=None, size=(40, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    kw = {"exif": exif.tobytes()} if exif is not None else {}
    Image.new("RGB", size, "red").save(path, "JPEG", **kw)
    return path


def camera_exif(model="TestCam", taken="2026:09:26 07:15:00"):
    exif = Image.Exif()
    exif[0x0110] = model
    exif.get_ifd(0x8769)[0x9003] = taken
    return exif


def manifest(collection, photos, tags=(), photographers=()):
    m = sqlite3.connect(collection / "manifest.sqlite")
    m.executescript(MANIFEST_SCHEMA)
    m.executemany("insert into photos(photo_id, tag_id, uid) values (?,?,?)", photos)
    m.executemany("insert into tags(tag_id, name) values (?,?)", tags)
    m.executemany("insert into photographers(uid, nickname) values (?,?)", photographers)
    m.commit()
    m.close()


def make_collection(tmp_path):
    c = tmp_path / "coll"
    outside = tmp_path / "elsewhere"
    jpeg(c / "photos" / "101.jpg", camera_exif())
    jpeg(c / "photos" / "102.jpg")
    (c / "photos" / "103.jpg").write_bytes(b"not an image at all")
    (c / "photos" / "104.jpg.part").write_bytes(b"partial")
    (c / "sub").mkdir()
    Image.new("RGB", (20, 10), "blue").save(c / "sub" / "shot.PNG")
    jpeg(outside / "105.jpg", size=(64, 48))
    (c / "photos" / "105.jpg").symlink_to(outside / "105.jpg")
    (c / "photos" / "notes.txt").write_text("x")
    manifest(c, [(101, 7, "u1"), (105, 8, "u2"), (999, 7, "u1")],
             tags=[(7, "finish line"), (8, "mountain")], photographers=[("u1", "cam-a"), ("u2", "cam-b")])
    return c


def rows(conn):
    conn.row_factory = sqlite3.Row
    return {r["relpath"]: dict(r) for r in conn.execute("select * from photos")}


def test_scan_indexes_images_with_exif_manifest_and_errors(tmp_path):
    c = make_collection(tmp_path)
    conn = db.connect(c)
    counts = scan(conn, c)
    got = rows(conn)

    assert counts == {"new": 5, "existing": 0, "errors": 1}
    assert set(got) == {"photos/101.jpg", "photos/102.jpg", "photos/103.jpg", "photos/105.jpg", "sub/shot.PNG"}

    exif = got["photos/101.jpg"]
    assert (exif["width"], exif["height"], exif["camera"]) == (40, 30, "TestCam")
    assert exif["taken_at"] == "2026-09-26 07:15:00"
    assert exif["taken_ts"] == datetime(2026, 9, 26, 7, 15).timestamp()
    assert (exif["photographer_uid"], exif["photographer"], exif["grp"], exif["album"]) == ("u1", "cam-a", "finish line", None)
    assert exif["source_photo_id"] == "101" and exif["status"] == "ok"

    plain = got["photos/102.jpg"]
    assert plain["taken_at"] is None and plain["taken_ts"] is None and plain["camera"] is None
    assert plain["photographer"] is None and plain["status"] == "ok"

    bad = got["photos/103.jpg"]
    assert bad["status"] == "error" and bad["error"] and bad["width"] is None

    link = got["photos/105.jpg"]
    assert (link["width"], link["height"], link["grp"], link["photographer"]) == (64, 48, "mountain", "cam-b")

    png = got["sub/shot.PNG"]
    assert (png["width"], png["height"], png["source_photo_id"], png["grp"]) == (20, 10, "shot", None)


def test_rerun_adds_nothing_and_leaves_rows_untouched(tmp_path):
    c = make_collection(tmp_path)
    conn = db.connect(c)
    scan(conn, c)
    conn.execute("update photos set camera = 'kept'")
    conn.commit()
    jpeg(c / "photos" / "106.jpg")

    counts = scan(conn, c)
    got = rows(conn)
    assert counts == {"new": 1, "existing": 5, "errors": 0}
    assert len(got) == 6
    assert {r["camera"] for p, r in got.items() if p != "photos/106.jpg"} == {"kept"}


def test_scan_without_manifest_leaves_join_null(tmp_path):
    c = tmp_path / "coll"
    jpeg(c / "101.jpg")
    conn = db.connect(c)
    assert scan(conn, c)["new"] == 1
    r = rows(conn)["101.jpg"]
    assert r["photographer_uid"] is None and r["grp"] is None


def test_index_files_are_not_scanned(tmp_path):
    c = tmp_path / "coll"
    jpeg(c / "1.jpg")
    for name in ["index.sqlite-wal", "manifest.sqlite-journal", "2.jpg-journal", "3.jpeg.part", "notes.txt"]:
        (c / name).write_bytes(b"x")
    conn = db.connect(c)
    scan(conn, c)
    assert set(rows(conn)) == {"1.jpg"}


def test_manifest_is_read_only_and_closed_before_image_reads(tmp_path, monkeypatch):
    c = make_collection(tmp_path)
    opened, reads = [], []
    real_connect = sqlite3.connect

    def spy(target, *a, **kw):
        conn = real_connect(target, *a, **kw)
        if "manifest.sqlite" in str(target):
            opened.append((str(target), conn))
        return conn

    real_open = stages.Image.open

    def open_checking(path, *a, **kw):
        (uri, conn), = opened
        assert uri.startswith("file:") and uri.endswith("?mode=ro")
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("select 1")
        reads.append(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    monkeypatch.setattr(stages.Image, "open", open_checking)
    assert scan(db.connect(c), c)["new"] == 5
    assert len(reads) == 5


def test_manifest_rows_added_during_walk_are_joined(tmp_path, monkeypatch):
    c = tmp_path / "coll"
    jpeg(c / "photos" / "101.jpg")
    manifest(c, [(101, 7, "u1")], tags=[(7, "finish line"), (8, "mountain")], photographers=[("u1", "cam-a")])
    real_find = stages.find_images

    def find_while_downloading(collection):
        m = sqlite3.connect(collection / "manifest.sqlite")
        m.execute("insert into photos(photo_id, tag_id, uid) values (201, 8, 'u1')")
        m.commit()
        m.close()
        jpeg(collection / "photos" / "201.jpg")
        return real_find(collection)

    monkeypatch.setattr(stages, "find_images", find_while_downloading)
    conn = db.connect(c)
    scan(conn, c)
    got = rows(conn)
    assert (got["photos/201.jpg"]["photographer"], got["photos/201.jpg"]["grp"]) == ("cam-a", "mountain")
    assert got["photos/101.jpg"]["grp"] == "finish line"


def test_exif_rotated_photo_stores_upright_size(tmp_path):
    c = tmp_path / "coll"
    for orientation in (3, 6, 8):
        exif = Image.Exif()
        exif[0x0112] = orientation
        jpeg(c / f"{orientation}.jpg", exif, size=(400, 300))
    conn = db.connect(c)
    scan(conn, c)
    got = rows(conn)
    assert (got["3.jpg"]["width"], got["3.jpg"]["height"]) == (400, 300)
    for name in ("6.jpg", "8.jpg"):
        assert (got[name]["width"], got[name]["height"]) == (300, 400)
        assert stages.models.load_image(c / name).size == (300, 400)


def test_cli_missing_collection_exits_nonzero(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["index", str(tmp_path / "nope")])
    assert e.value.code not in (0, None)
    assert "nope" in str(e.value.code)


def test_cli_index_runs_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    monkeypatch.setattr(cli.models, "detect_persons", lambda images: [[] for _ in images])
    monkeypatch.setattr(cli.models, "embed_images", lambda images: np.ones((len(images), 4), np.float32))
    monkeypatch.setattr(cli.models, "read_text", lambda img: [])
    c = tmp_path / "coll"
    jpeg(c / "1.jpg")
    cli.main(["index", str(c)])
    assert set(rows(sqlite3.connect(c / "index.sqlite"))) == {"1.jpg"}


def held_lock(c):
    f = open(c / "index.lock", "a")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def test_cli_index_refuses_while_another_run_holds_the_lock(tmp_path, monkeypatch):
    c = tmp_path / "coll"
    jpeg(c / "1.jpg")
    ran = []
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    monkeypatch.setattr(stages, "scan", lambda *a: ran.append("scan"))
    with held_lock(c):
        with pytest.raises(SystemExit) as e:
            cli.main(["index", str(c)])
    msg = str(e.value.code)
    assert "another `photofinder index` run is active" in msg and str(c) in msg and "\n" not in msg
    assert ran == []
    assert not (c / "index.sqlite").exists()


def test_cli_index_lock_held_by_other_process_blocks_and_is_released_after_run(tmp_path, monkeypatch):
    c = tmp_path / "coll"
    jpeg(c / "1.jpg")
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    monkeypatch.setattr(cli.models, "detect_persons", lambda images: [[] for _ in images])
    monkeypatch.setattr(cli.models, "embed_images", lambda images: np.ones((len(images), 4), np.float32))
    holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys; f = open(sys.argv[1], 'a'); "
                               "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); sys.stdin.read()",
                               str(c / "index.lock")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline() == "held\n"
        with pytest.raises(SystemExit) as e:
            cli.main(["index", str(c)])
        assert "another `photofinder index` run is active" in str(e.value.code)
        assert not (c / "index.sqlite").exists()
    finally:
        holder.communicate("")
    cli.main(["index", str(c)])
    assert set(rows(sqlite3.connect(c / "index.sqlite"))) == {"1.jpg"}
    with held_lock(c):
        pass
    assert "index.lock" not in " ".join(stages.find_images(c))
