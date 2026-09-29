import json
import shutil

import pytest

from photofinder import cli, config, originals, races
from test_search import Fakes, query_photo, search_index


@pytest.fixture(autouse=True)
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root)
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    return root


def registry(root):
    return json.loads((root / "races.json").read_text(encoding="utf-8"))


def fail(*argv):
    with pytest.raises(SystemExit) as e:
        cli.main(list(argv))
    assert e.value.code not in (0, None)
    return str(e.value.code)


def test_race_add_cli_writes_registry(data_root, capsys):
    cli.main(["race", "add", "2026-x", "2026 贡嘎100"])
    assert registry(data_root) == {"races": [{"slug": "2026-x", "name": "2026 贡嘎100", "albums": []}]}
    assert "registered race 2026-x" in capsys.readouterr().out
    assert [p.name for p in data_root.iterdir()] == ["races.json"]
    cli.main(["race", "add", "chongli168", "Chongli"])
    assert [r.slug for r in races.load().races] == ["2026-x", "chongli168"]


@pytest.mark.parametrize("slug", ["2026_X", "-x", "", "a/b", "Gongga", "x y", "x\n"])
def test_race_add_rejects_bad_slug(data_root, slug):
    with pytest.raises(races.RaceError, match="race slug"):
        races.add_race(slug, "Name")
    assert "race slug" in fail("race", "add", "2026_X", "Name")
    assert not (data_root / "races.json").exists()


def test_race_add_rejects_duplicate_slug(data_root):
    races.add_race("2026-x", "First")
    assert fail("race", "add", "2026-x", "Second") == "race '2026-x' is already registered"
    assert registry(data_root)["races"][0]["name"] == "First"


def test_race_add_rejects_empty_name(data_root):
    assert "name" in fail("race", "add", "2026-x", "  ")


def test_save_is_atomic_when_replace_fails(data_root, monkeypatch):
    races.add_race("2026-x", "First")
    before = (data_root / "races.json").read_bytes()

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(races.os, "replace", boom)
    with pytest.raises(OSError):
        races.add_race("2026-y", "Second")
    assert (data_root / "races.json").read_bytes() == before
    assert [p.name for p in data_root.iterdir()] == ["races.json"]


@pytest.mark.parametrize("url, platform, site_id", [
    ("https://www.yipai360.com/photolivepc/?orderId=20260920190645388324", "yipai", "20260920190645388324"),
    ("https://live.pailixiang.com/album/a13800138000", "pailixiang", "a13800138000"),
    ("https://live.pailixiang.com/album/a13800138000/", "pailixiang", "a13800138000"),
    ("https://www.xxpie.com/m/album?album_id=66c1f0e2ab&is_visited=0", "xxpie", "66c1f0e2ab"),
    ("https://live.photoplus.cn/live/39352660?accessFrom=live#/live", "photoplus", "39352660"),
])
def test_add_album_parses_each_platform(data_root, url, platform, site_id):
    races.add_race("2026-x", "X")
    album = races.add_album("2026-x", url, title="Album")
    assert (album.platform, album.site_id, album.key) == (platform, site_id, f"{platform}-{site_id}")
    assert registry(data_root)["races"][0]["albums"] == [
        {"key": f"{platform}-{site_id}", "platform": platform, "site_id": site_id, "url": url, "title": "Album"}]
    assert races.race("2026-x").albums == [album]
    assert races.album_dir("2026-x", album.key) == data_root / "races" / "2026-x" / "albums" / album.key


@pytest.mark.parametrize("url", [
    "https://example.com/album/a1",
    "https://example.com/live/39352660",
    "https://notxxpie.com/m/album?album_id=1",
    "https://www.yipai360.com/photolivepc/",
    "https://www.yipai360.com/photolivepc/?orderId=../../x",
    "https://live.pailixiang.com/album/13800138000",
    "https://www.xxpie.com/m/album",
    "https://live.photoplus.cn/live/",
    "not a url",
])
def test_add_album_rejects_unknown_or_incomplete_urls(data_root, url):
    races.add_race("2026-x", "X")
    with pytest.raises(races.RaceError):
        races.add_album("2026-x", url)
    assert races.race("2026-x").albums == []


def test_add_album_duplicate_key_names_owning_race(data_root):
    races.add_race("2026-a", "A")
    races.add_race("2026-b", "B")
    races.add_album("2026-a", "https://live.photoplus.cn/live/39352660")
    for slug in ("2026-b", "2026-a"):
        with pytest.raises(races.RaceError, match="photoplus-39352660 already belongs to race 2026-a"):
            races.add_album(slug, "https://live.photoplus.cn/live/39352660?accessFrom=live#/live")
    assert [len(r.albums) for r in races.load().races] == [1, 0]


def test_add_album_to_unknown_race(data_root):
    with pytest.raises(races.RaceError, match="no race 'nope'"):
        races.add_album("nope", "https://live.photoplus.cn/live/39352660")
    with pytest.raises(races.RaceError, match="no race 'nope'"):
        races.race("nope")


def capture(monkeypatch, command):
    seen = []
    monkeypatch.setattr(cli, f"cmd_{command}", lambda args: seen.append(args.collection))
    return seen


@pytest.mark.parametrize("command", ["index", "search", "eval", "serve"])
def test_commands_resolve_a_race_slug(data_root, monkeypatch, command):
    races.add_race("2026-x", "X")
    races.race_dir("2026-x").mkdir(parents=True)
    seen = capture(monkeypatch, command)
    cli.main([command, "2026-x"])
    assert seen == [data_root / "races" / "2026-x"]


@pytest.mark.parametrize("command", ["index", "search", "eval", "serve"])
def test_commands_still_take_a_directory(tmp_path, monkeypatch, command):
    coll = tmp_path / "coll"
    coll.mkdir()
    seen = capture(monkeypatch, command)
    cli.main([command, str(coll)])
    assert seen == [coll]


def test_existing_directory_wins_over_a_slug(tmp_path, monkeypatch):
    races.add_race("coll", "X")
    races.race_dir("coll").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "coll").mkdir()
    seen = capture(monkeypatch, "index")
    cli.main(["index", "coll"])
    assert [p.resolve() for p in seen] == [(tmp_path / "coll").resolve()]


def test_unknown_slug_is_a_clear_error(data_root, monkeypatch):
    seen = capture(monkeypatch, "index")
    assert fail("index", "2026-nope") == "2026-nope is neither a directory nor a registered race (races: none registered)"
    races.add_race("2026-x", "X")
    assert fail("search", "2026-nope").endswith("(races: 2026-x)")
    assert seen == []


def test_registered_race_without_directory_is_a_clear_error(data_root, monkeypatch):
    races.add_race("2026-x", "X")
    seen = capture(monkeypatch, "index")
    msg = fail("index", "2026-x")
    assert msg.startswith("race 2026-x has no directory") and str(races.race_dir("2026-x")) in msg
    assert seen == []


def test_race_exports_go_under_the_race_slug(data_root, tmp_path, capsys, monkeypatch):
    c, conn, _ = search_index(tmp_path)
    conn.close()
    races.add_race("2026-x", "X")
    race = races.race_dir("2026-x")
    race.parent.mkdir(parents=True)
    shutil.move(c, race)
    Fakes(monkeypatch)
    cli.main(["search", "2026-x", "--photo", str(query_photo(tmp_path))])
    [sheet] = (data_root / "exports").glob("*.jpg")
    assert sheet.name.startswith("2026-x-search-")
    assert originals.profile_folder(race, "Me") == data_root / "exports" / "2026-x" / "Me"
    with cli.lock_index(race):
        assert (race / "index.lock").is_file()
