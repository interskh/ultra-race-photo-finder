import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from photofinder import cli, config, db, race_import, races
from photofinder.index.stages import scan
from test_race_scan import yipai_album
from test_scan import camera_exif, jpeg

ORDER = "1001"
URL = f"https://www.yipai360.com/photolivepc/?orderId={ORDER}"
SLUG = "2026-x"
KEY = f"yipai-{ORDER}"
STEPS = ["backup", "move_collection", "add_catalog", "move_index", "rewrite_index", "move_exports", "fix_csvs",
         "repoint_links", "verify", "register"]
LEGACY = db.SCHEMA.replace("album text, album_key text, grp text,", "album text,")
T = "2026-09-28 10:00:00"
BOM = "﻿"


@pytest.fixture
def root(tmp_path, monkeypatch):
    return use_root(monkeypatch, tmp_path / "data")


def use_root(monkeypatch, root):
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DATA_ROOT", root)
    return root


def make_collection(root):
    src = root / "yipai" / ORDER
    for pid in (101, 102, 103):
        jpeg(src / "photos" / f"{pid}.jpg", camera_exif() if pid == 101 else None)
    yipai_album(src, [(101, ORDER, 7, "123", "A.JPG"), (102, ORDER, 8, "u2", "B.JPG"), (103, ORDER, 7, None, "C.JPG")],
                [(7, "终点"), (8, "起点")], [("123", "cam-a"), ("u2", "cam-b")], view=False)
    conn = sqlite3.connect(src / "index.sqlite")
    conn.execute("pragma journal_mode=wal")
    conn.executescript(LEGACY)
    conn.executemany("insert into photos(id, relpath, source_photo_id, photographer_uid, photographer, album) "
                     "values (?,?,?,?,?,?)", [(1, "photos/101.jpg", "101", "123", "cam-a", "终点"),
                                              (2, "photos/102.jpg", "102", "u2", "cam-b", "起点"),
                                              (3, "photos/103.jpg", "103", None, None, "终点")])
    conn.executemany("insert into persons(id, photo_id) values (?,?)", [(1, 1), (2, 1), (3, 2), (4, 3)])
    conn.executemany("insert into profiles(id, name, created_at) values (?,?,?)", [(1, "Me", T), (2, "Pat", T)])
    conn.executemany("insert into labels(profile_id, person_id, label, created_at) values (?,?,?,?)",
                     [(1, 1, "me", T), (1, 2, "not_me", T), (2, 3, "me", T), (2, 4, "not_me", T)])
    conn.commit()
    conn.close()
    (src / "index.lock").touch()
    (src / ".download.lock").touch()
    (root / "serve.lock").write_text("pid 1, http://127.0.0.1:8000/, old")
    real, exports = src.resolve(), (root / "exports" / ORDER).resolve()
    for profile, pid in (("Me", 101), ("Pat", 102)):
        jpeg(exports / profile / "originals" / f"{pid}.jpg")
        (exports / profile / "photos.csv").write_bytes(
            (f"{BOM}source_photo_id,preview_path,original_path,status\r\n"
             f"{pid},{real}/photos/{pid}.jpg,{exports}/{profile}/originals/{pid}.jpg,downloaded\r\n"
             f"103,{real}/photos/103.jpg,,\r\n").encode())
    subsets = root / "subsets"
    (subsets / "full").mkdir(parents=True)
    os.symlink(f"{real}/photos", subsets / "full" / "photos")
    part = subsets / "part"
    (part / "photos").mkdir(parents=True)
    os.symlink(f"{real}/photos/101.jpg", part / "photos" / "101.jpg")
    os.symlink(f"{real}0/photos/9.jpg", part / "photos" / "9.jpg")
    os.symlink("/elsewhere/x.jpg", part / "photos" / "x.jpg")
    os.symlink(f"{real}/manifest.sqlite", part / "manifest.sqlite")
    return src


def run(src, **kw):
    return race_import.run(SLUG, "2026 X", src, URL, say=lambda s: None, **kw)


def norm(root, s):
    return s.replace(str(root.resolve()), "<root>").replace(str(root), "<root>")


def tree(root, skip=("backups",)):
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in skip]
        for name in dirs + files:
            path = os.path.join(d, name)
            rel = os.path.relpath(path, root)
            if os.path.islink(path):
                out[rel] = ("link", norm(root, os.readlink(path)))
            elif os.path.isdir(path):
                out[rel] = ("dir",)
            elif name.endswith((".sqlite", "-wal", "-shm")):
                out[rel] = ("db",)
            else:
                with open(path, "rb") as f:
                    out[rel] = ("file", norm(root, f.read().decode("utf-8", "replace")))
    return out


def raw_tree(root):
    out = {}
    for d, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(d, name)
            if os.path.islink(path):
                out[path] = os.readlink(path)
            elif os.path.isfile(path):
                with open(path, "rb") as f:
                    out[path] = hashlib.sha256(f.read()).hexdigest()
            else:
                out[path] = "dir"
    return out


def final_state(root):
    race = root / "races" / SLUG
    with closing(sqlite3.connect(race / "index.sqlite")) as conn:
        photos = conn.execute("select id, relpath, source_photo_id, photographer_uid, photographer, album, album_key, "
                              "grp from photos order by id").fetchall()
        labels = conn.execute("select profile_id, person_id, label, created_at from labels order by 1, 2").fetchall()
        profiles = conn.execute("select * from profiles order by id").fetchall()
    with closing(sqlite3.connect(race / "albums" / KEY / "manifest.sqlite")) as m:
        catalog = m.execute("select source_id, photographer_uid, group_name from catalog order by 1").fetchall()
    return {"photos": photos, "labels": labels, "profiles": profiles, "catalog": catalog, "tree": tree(root),
            "registry": json.loads((root / "races.json").read_text(encoding="utf-8"))}


@pytest.fixture
def reference(tmp_path, monkeypatch):
    ref = use_root(monkeypatch, tmp_path / "ref" / "data")
    run(make_collection(ref))
    state = final_state(ref)
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data")
    return state


def test_import_moves_collection_into_race(root):
    src = make_collection(root)
    out = run(src)
    race, album, exports = root / "races" / SLUG, root / "races" / SLUG / "albums" / KEY, root / "exports" / SLUG
    assert not src.exists() and not (root / "exports" / ORDER).exists()
    assert (out["photos"], out["persons"], out["profiles"], out["labels"], out["symlinks"]) == (3, 4, 2, 4, 3)
    s = final_state(root)
    assert s["photos"] == [
        (1, f"albums/{KEY}/photos/101.jpg", "101", "yipai:123", "cam-a", "2026 X", KEY, "终点"),
        (2, f"albums/{KEY}/photos/102.jpg", "102", "yipai:u2", "cam-b", "2026 X", KEY, "起点"),
        (3, f"albums/{KEY}/photos/103.jpg", "103", None, None, "2026 X", KEY, "终点")]
    assert s["labels"] == [(1, 1, "me", T), (1, 2, "not_me", T), (2, 3, "me", T), (2, 4, "not_me", T)]
    assert s["profiles"] == [(1, "Me", T), (2, "Pat", T)]
    assert s["catalog"] == [("101", "123", "终点"), ("102", "u2", "起点"), ("103", None, "终点")]
    assert s["registry"] == {"races": [{"slug": SLUG, "name": "2026 X", "albums": [
        {"key": KEY, "platform": "yipai", "site_id": ORDER, "url": URL, "title": "2026 X"}]}]}
    assert sorted(p.name for p in race.iterdir()) == ["albums", "index.lock", "index.sqlite"]
    assert {"index.lock", ".download.lock", "manifest.sqlite", "photos"} <= {p.name for p in album.iterdir()}
    a, e = album.resolve(), exports.resolve()
    me = (exports / "Me" / "photos.csv").read_bytes().decode()
    assert me == (f"{BOM}source_photo_id,preview_path,original_path,status\r\n"
                  f"101,{a}/photos/101.jpg,{e}/Me/originals/101.jpg,downloaded\r\n103,{a}/photos/103.jpg,,\r\n")
    assert (exports / "Pat" / "originals" / "102.jpg").is_file()
    t = s["tree"]
    assert t["subsets/full/photos"] == ("link", f"<root>/races/{SLUG}/albums/{KEY}/photos")
    assert t["subsets/part/photos/101.jpg"] == ("link", f"<root>/races/{SLUG}/albums/{KEY}/photos/101.jpg")
    assert t["subsets/part/manifest.sqlite"] == ("link", f"<root>/races/{SLUG}/albums/{KEY}/manifest.sqlite")
    assert t["subsets/part/photos/9.jpg"] == ("link", f"<root>/yipai/{ORDER}0/photos/9.jpg")
    assert t["subsets/part/photos/x.jpg"] == ("link", "/elsewhere/x.jpg")
    assert (root / "subsets" / "full" / "photos" / "102.jpg").is_file()
    [backup] = (root / "backups").iterdir()
    assert backup.name.startswith(f"{ORDER}-index-") and backup.suffix == ".sqlite" and backup == Path(out["backup"])
    with sqlite3.connect(backup) as b:
        assert b.execute("select relpath, album from photos where id = 1").fetchone() == ("photos/101.jpg", "终点")
    with db.connect(race) as conn:
        assert scan(conn, race) == {"new": 0, "existing": 3, "errors": 0, "skipped": 0}


@pytest.mark.parametrize("step", STEPS + ["migrated_only"])
def test_rerun_after_crash_completes_identically(root, reference, monkeypatch, step):
    src = make_collection(root)
    name = "rewrite_index" if step == "migrated_only" else step
    real = getattr(race_import, name)

    def crash(p, *a):
        if step == "migrated_only":
            db.connect(p.race).close()
        raise RuntimeError("crash")
    monkeypatch.setattr(race_import, name, crash)
    with pytest.raises(RuntimeError):
        run(src)
    assert not (root / "races.json").exists()
    monkeypatch.setattr(race_import, name, real)
    run(src)
    assert final_state(root) == reference


def test_second_import_refuses_and_changes_nothing(root):
    src = make_collection(root)
    run(src)
    before = raw_tree(root)
    with pytest.raises(race_import.ImportRefused, match="already registered"):
        run(src)
    with pytest.raises(race_import.ImportRefused, match=f"{KEY} is already imported into {SLUG}"):
        race_import.run("2026-y", "Y", src, URL, say=lambda s: None)
    assert raw_tree(root) == before


def hold(path):
    f = open(path, "a")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


@pytest.mark.parametrize("lock, who", [(f"yipai/{ORDER}/index.lock", "photofinder index"),
                                       (f"yipai/{ORDER}/.download.lock", "download"),
                                       ("serve.lock", "server")])
def test_refuses_while_a_lock_is_held(root, lock, who):
    src = make_collection(root)
    before = raw_tree(root)
    with hold(root / lock):
        with pytest.raises(race_import.ImportRefused, match=who):
            run(src)
        assert raw_tree(root) == before
    run(src)
    assert (root / "races.json").is_file()


@pytest.mark.parametrize("lock", [f"races/{SLUG}/albums/{KEY}/index.lock", f"races/{SLUG}/albums/{KEY}/.download.lock",
                                  f"races/{SLUG}/index.lock"])
def test_resume_refuses_while_a_lock_is_held(root, monkeypatch, lock):
    src = make_collection(root)
    real = race_import.rewrite_index
    monkeypatch.setattr(race_import, "rewrite_index", lambda p: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        run(src)
    monkeypatch.setattr(race_import, "rewrite_index", real)
    before = raw_tree(root)
    with hold(root / lock):
        with pytest.raises(race_import.ImportRefused, match="holds"):
            run(src)
        assert raw_tree(root) == before
    run(src)


def test_refuses_while_existing_race_dir_is_locked(root):
    src = make_collection(root)
    (root / "races" / SLUG).mkdir(parents=True)
    with hold(root / "races" / SLUG / "index.lock"):
        with pytest.raises(race_import.ImportRefused, match="on the race"):
            run(src)
    assert src.is_dir()


def test_indexer_cannot_start_on_the_race_during_the_move(root, monkeypatch, reference):
    src = make_collection(root)
    real, indexer = race_import.move_collection, []

    def indexer_starts_then_move(p):
        p.album.parent.mkdir(parents=True, exist_ok=True)
        try:
            with hold(p.race / "index.lock"):
                db.connect(p.race).close()
                indexer.append("indexed")
        except BlockingIOError:
            indexer.append("refused")
        real(p)
    monkeypatch.setattr(race_import, "move_collection", indexer_starts_then_move)
    run(src)
    assert indexer == ["refused"]
    assert final_state(root) == reference


def test_refuses_a_symlinked_collection(root, tmp_path):
    real = make_collection(root)
    elsewhere = tmp_path / "elsewhere" / ORDER
    elsewhere.parent.mkdir()
    os.rename(real, elsewhere)
    os.symlink(elsewhere, real)
    before = raw_tree(root)
    with pytest.raises(race_import.ImportRefused, match="is a symlink"):
        run(real)
    assert raw_tree(root) == before and elsewhere.is_dir()
    os.unlink(real)
    album = root / "races" / SLUG / "albums" / KEY
    album.parent.mkdir(parents=True)
    os.symlink(elsewhere, album)
    with pytest.raises(race_import.ImportRefused, match="is a symlink"):
        run(real)


@pytest.mark.parametrize("url, msg", [
    ("https://www.yipai360.com/photolivepc/?orderId=1002", "does not match"),
    ("https://live.photoplus.cn/live/1001", "is a photoplus album"),
    ("https://example.com/x", "unsupported album URL"),
])
def test_refuses_wrong_url(root, url, msg):
    src = make_collection(root)
    before = raw_tree(root)
    with pytest.raises(race_import.ImportRefused, match=msg):
        race_import.run(SLUG, "2026 X", src, url, say=lambda s: None)
    assert raw_tree(root) == before


def test_refuses_manifest_of_another_order(root):
    src = make_collection(root)
    with sqlite3.connect(src / "manifest.sqlite") as m:
        m.execute("update photos set order_id = '9999' where photo_id = 103")
    with pytest.raises(race_import.ImportRefused, match="order ids"):
        run(src)
    assert src.is_dir() and not (root / "races").exists()


@pytest.mark.parametrize("setup, msg", [
    (lambda root, src: (root / "races" / SLUG / "albums" / KEY).mkdir(parents=True), "both"),
    (lambda root, src: (root / "exports" / SLUG).mkdir(parents=True), "both"),
    (lambda root, src: ((root / "races" / SLUG).mkdir(parents=True),
                        (root / "races" / SLUG / "index.sqlite").touch()), "ambiguous"),
    (lambda root, src: (src / "index.sqlite").unlink(), "no index.sqlite"),
    (lambda root, src: races.add_race(SLUG, "Other"), "already registered"),
])
def test_refuses_inconsistent_state(root, setup, msg):
    src = make_collection(root)
    setup(root, src)
    before = raw_tree(root)
    with pytest.raises(race_import.ImportRefused, match=msg):
        run(src)
    assert raw_tree(root) == before


def test_leftover_partial_backups_are_removed(root):
    src = make_collection(root)
    backups = root / "backups"
    backups.mkdir()
    stale = [backups / f"{ORDER}-index-20260101-000000.sqlite.part",
             backups / f"{ORDER}-index-20260101-000000.sqlite.part-journal"]
    keep = [backups / f"{ORDER}-index-20260101-000000.sqlite", backups / "9999-index-20260101-000000.sqlite.part",
            backups / "index-1001-20260101-000000.sqlite"]
    for f in stale + keep:
        f.write_bytes(b"x")
    out = run(src)
    assert not any(f.exists() for f in stale)
    assert all(f.read_bytes() == b"x" for f in keep)
    assert sorted(p.name for p in backups.iterdir()) == sorted([f.name for f in keep] + [Path(out["backup"]).name])


def test_verify_failure_stops_before_registering(root):
    src = make_collection(root)
    (src / "photos" / "103.jpg").unlink()
    with pytest.raises(race_import.ImportRefused, match="1 indexed photos missing"):
        run(src)
    assert not (root / "races.json").exists()
    assert len(list((root / "backups").iterdir())) == 1


def without_profiles(src):
    with closing(sqlite3.connect(src / "index.sqlite")) as conn:
        conn.execute("delete from labels")
        conn.execute("delete from profiles")
        conn.commit()


def test_index_without_profiles_imports_with_the_default_profile(root):
    src = make_collection(root)
    without_profiles(src)
    assert run(src)["profiles"] == 1
    s = final_state(root)
    assert [name for _, name, _ in s["profiles"]] == [db.DEFAULT_PROFILE] and s["labels"] == []


@pytest.mark.parametrize("change", ["update profiles set name = 'Pat'",
                                    "insert into profiles(name, created_at) values ('Pat', 'x')",
                                    "insert into labels(profile_id, person_id, label, created_at) values (1, 1, 'me', 'x')"])
def test_verify_rejects_other_changes_to_an_index_without_profiles(root, monkeypatch, change):
    src = make_collection(root)
    without_profiles(src)
    real = race_import.rewrite_index

    def also_change(p):
        real(p)
        with closing(sqlite3.connect(p.race / "index.sqlite")) as conn:
            conn.execute(change)
            conn.commit()
    monkeypatch.setattr(race_import, "rewrite_index", also_change)
    with pytest.raises(race_import.ImportRefused, match="differ from the backup"):
        run(src)
    assert not (root / "races.json").exists()


def test_verify_detects_changed_labels(root, monkeypatch):
    src = make_collection(root)
    real = race_import.rewrite_index

    def also_delete_a_label(p):
        real(p)
        with sqlite3.connect(p.race / "index.sqlite") as conn:
            conn.execute("delete from labels where person_id = 4")
    monkeypatch.setattr(race_import, "rewrite_index", also_delete_a_label)
    with pytest.raises(race_import.ImportRefused, match=r"labels differ from the backup \(4 before, 3 after\)"):
        run(src)
    assert not (root / "races.json").exists()


def test_cli_import_and_refusal(root, monkeypatch, capsys):
    src = make_collection(root)
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: pytest.fail("loaded model env"))
    cli.main(["race", "import", SLUG, "2026 X", str(src), "--url", URL, "--title", "FUGA X"])
    out = capsys.readouterr().out
    assert f"into race {SLUG}" in out and "labels 4 (unchanged)" in out and "3 subset symlinks" in out
    assert races.race(SLUG).albums[0].title == "FUGA X"
    with db.connect(root / "races" / SLUG) as conn:
        assert {a for (a,) in conn.execute("select album from photos")} == {"FUGA X"}
    with pytest.raises(SystemExit) as e:
        cli.main(["race", "import", SLUG, "2026 X", str(src), "--url", URL])
    assert "already registered" in str(e.value.code)


def test_cli_checks_the_effective_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "missing")
    with pytest.raises(SystemExit) as e:
        cli.main(["race", "add", SLUG, "X"])
    assert "missing does not exist" in str(e.value.code)


def test_sigkilled_import_completes_on_rerun(root, reference):
    src = make_collection(root)
    env = {**os.environ, "PHOTOFINDER_DATA_ROOT": str(root), race_import.KILL_AFTER: "rewrite_index"}
    argv = [sys.executable, "-m", "photofinder.cli", "race", "import", SLUG, "2026 X", str(src), "--url", URL]
    killed = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    assert f"{race_import.KILL_AFTER}=rewrite_index is set (test hook)" in killed.stderr
    assert "done: rewrite_index" in killed.stdout and "done: move_exports" not in killed.stdout
    assert not src.exists() and (root / "exports" / ORDER).is_dir() and not (root / "races.json").exists()
    del env[race_import.KILL_AFTER]
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert final_state(root) == reference
