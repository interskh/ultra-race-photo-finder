import logging
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from photofinder import cli, config, races
from photofinder.sources import yipai
from photofinder.sources.base import AlbumDownloader
from test_pailixiang import LISTING, FakePlx
from test_pailixiang import client_for as plx_client
from test_yipai import ORDER, FakeSite

REPO = Path(__file__).resolve().parents[1]
YIPAI_URL = f"https://www.yipai360.com/photolivepc/?orderId={ORDER}"
OTHER_URL = "https://www.yipai360.com/photolivepc/?orderId=ORD2"
PLX_URL = "https://live.pailixiang.com/album/a13800138000"
PP_URL = "https://live.photoplus.cn/live/39352660?accessFrom=live#/live"


@pytest.fixture(autouse=True)
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(config, "DATA_ROOT", root)
    return root


@pytest.fixture
def site(monkeypatch):
    site = FakeSite(n_photos=3, page_size=2)
    made = []

    def downloader(client, order_id, out_dir):
        made.append(order_id)
        return yipai.Downloader(client, order_id, out_dir, page_size=site.page_size, sleep=lambda s: None)
    monkeypatch.setattr(cli, "yipai_client", lambda: httpx.Client(transport=httpx.MockTransport(site)))
    monkeypatch.setattr(cli, "yipai_downloader", downloader)
    site.made = made
    return site


def album_handlers():
    return [h for h in logging.getLogger().handlers
            if isinstance(h, logging.FileHandler) and h.baseFilename.endswith("download.log")]


def race(*urls):
    races.add_race("2026-x", "X")
    return [races.add_album("2026-x", u) for u in urls]


def fail(*argv):
    with pytest.raises(SystemExit) as e:
        cli.main(list(argv))
    assert e.value.code not in (0, None)
    return e.value.code


def test_downloads_yipai_album_into_the_race(data_root, site, caplog):
    caplog.set_level(logging.INFO)
    race(YIPAI_URL)
    cli.main(["download", "2026-x"])
    album = data_root / "races" / "2026-x" / "albums" / f"yipai-{ORDER}"
    assert sorted(p.name for p in (album / "photos").iterdir()) == ["1.jpg", "2.jpg", "3.jpg"]
    assert (album / "manifest.sqlite").is_file()
    assert f"yipai-{ORDER} finished" in (album / "download.log").read_text()
    assert not album_handlers()
    assert not (data_root / "yipai").exists()


def test_production_pacing_is_unchanged(monkeypatch):
    seen = {}
    monkeypatch.setattr(yipai, "Downloader", lambda client, order_id, out_dir, **kw: seen.update(kw))
    cli.yipai_downloader(None, ORDER, Path("unused"))
    assert seen == {"concurrency": 6, "page_delay": 3.0}


def test_album_key_selects_one_album(data_root, site):
    race(YIPAI_URL, OTHER_URL)
    cli.main(["download", "2026-x", "yipai-ORD2"])
    assert site.made == ["ORD2"]
    assert not (data_root / "races" / "2026-x" / "albums" / f"yipai-{ORDER}").exists()


def test_every_album_runs_in_order(site):
    race(YIPAI_URL, OTHER_URL)
    cli.main(["download", "2026-x"])
    assert site.made == [ORDER, "ORD2"]


def test_unknown_race_is_refused(data_root, site):
    race(YIPAI_URL)
    assert "races: 2026-x" in fail("download", "2026-y")
    assert site.made == []
    assert not (data_root / "races").exists()


def test_unknown_album_key_lists_the_keys(data_root, site):
    race(YIPAI_URL)
    msg = fail("download", "2026-x", "yipai-nope")
    assert f"albums: yipai-{ORDER}" in msg
    assert site.made == []
    assert not (data_root / "races").exists()


def test_unsupported_platform_album_is_skipped(site, capsys):
    race(PP_URL, YIPAI_URL)
    cli.main(["download", "2026-x"])
    assert "skipping photoplus-39352660: photoplus downloads are not supported yet" in capsys.readouterr().out
    assert site.made == [ORDER]


@pytest.fixture
def plx(monkeypatch):
    plx = FakePlx()
    made = []

    def downloader(client, adapter, out_dir, **kw):
        made.append(out_dir.name)
        return AlbumDownloader(client, adapter, out_dir, sleep=lambda s: None, **kw)
    monkeypatch.setattr(cli, "album_client", lambda platform: plx_client(plx))
    monkeypatch.setattr(cli, "album_downloader", downloader)
    plx.made = made
    return plx


def test_downloads_pailixiang_album_into_the_race(data_root, plx, site, caplog):
    caplog.set_level(logging.INFO)
    race(PLX_URL, YIPAI_URL)
    for _ in range(2):
        cli.main(["download", "2026-x"])
    album = data_root / "races" / "2026-x" / "albums" / "pailixiang-a13800138000"
    assert sorted(p.name for p in (album / "photos").iterdir()) == sorted(f"{p['ID']}.jpg" for p in LISTING["Data"])
    assert sum(plx.images.values()) == 3
    assert plx.made == ["pailixiang-a13800138000"] * 2 and site.made == [ORDER] * 2
    assert "pailixiang-a13800138000 finished" in (album / "download.log").read_text()
    assert not album_handlers()


def test_album_pacing_is_the_new_platform_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "AlbumDownloader", lambda client, adapter, out_dir, **kw: seen.update(kw))
    cli.album_downloader(None, None, Path("unused"))
    assert seen == {"concurrency": 4, "page_delay": 3.0, "img_delay": (0.2, 0.6), "tries": 5,
                    "max_consecutive_failures": 20}


def test_pailixiang_client_sends_the_site_headers():
    with cli.album_client("pailixiang") as client:
        assert client.headers["referer"] == "https://live.pailixiang.com/"
        assert client.headers["origin"] == "https://live.pailixiang.com"
        assert client.headers["content-type"] == "application/json;charset=UTF-8"


def test_blocked_pailixiang_album_stops_the_whole_run(data_root, plx, site, monkeypatch):
    race(PLX_URL, YIPAI_URL)
    monkeypatch.setattr(cli, "album_downloader", lambda client, adapter, out_dir: AlbumDownloader(
        client, adapter, out_dir, tries=1, max_consecutive_failures=1, sleep=lambda s: None))
    monkeypatch.setattr(cli, "album_client", lambda platform: httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(401) if req.url.host == "img.pailixiang.com" else plx(req))))
    assert fail("download", "2026-x") == 2
    assert site.made == []
    log = (data_root / "races" / "2026-x" / "albums" / "pailixiang-a13800138000" / "download.log").read_text()
    assert "rerun later to resume" in log
    assert not album_handlers()


def test_incomplete_album_exits_nonzero_after_the_rest(site):
    race(YIPAI_URL, OTHER_URL)
    site.image_handler = lambda req: httpx.Response(404) if req.url.path == "/p/1" else httpx.Response(200, content=b"\xff\xd8" + b"x" * 2000 + b"\xff\xd9")
    assert "ORD1" in fail("download", "2026-x")
    assert site.made == [ORDER, "ORD2"]


def test_blocked_stops_the_whole_run(data_root, site, monkeypatch):
    race(YIPAI_URL, OTHER_URL)
    site.image_handler = lambda req: httpx.Response(401)
    real = yipai.Downloader

    def breaker(client, order_id, out_dir):
        site.made.append(order_id)
        return real(client, order_id, out_dir, page_size=2, tries=1, max_consecutive_failures=1, sleep=lambda s: None)
    monkeypatch.setattr(cli, "yipai_downloader", breaker)
    assert fail("download", "2026-x") == 2
    assert site.made == [ORDER]
    log = (data_root / "races" / "2026-x" / "albums" / f"yipai-{ORDER}" / "download.log").read_text()
    assert "rerun later to resume" in log
    assert not album_handlers()


def test_already_running_album_stops_the_run(data_root, site):
    race(YIPAI_URL)
    album = data_root / "races" / "2026-x" / "albums" / f"yipai-{ORDER}"
    album.mkdir(parents=True)
    holder = yipai.Downloader(httpx.Client(transport=httpx.MockTransport(site)), ORDER, album)
    holder.acquire_lock()
    try:
        assert "another download is already running" in fail("download", "2026-x")
    finally:
        holder.close()


def test_yipai_module_refuses_registered_order(data_root):
    race(YIPAI_URL)
    with pytest.raises(SystemExit) as e:
        yipai.main([ORDER])
    assert "scripts/download.sh 2026-x" in str(e.value.code)
    assert not (data_root / "yipai").exists()


def test_yipai_refusal_is_none_for_unregistered_order():
    race(YIPAI_URL)
    assert yipai.refusal("ORD2") is None


def unfinished_import(root):
    album = root / "races" / "2026-x" / "albums" / f"yipai-{ORDER}"
    album.mkdir(parents=True)
    return album


def test_yipai_module_refuses_order_of_an_unfinished_import(data_root):
    unfinished_import(data_root)
    with pytest.raises(SystemExit) as e:
        yipai.main([ORDER])
    assert "import into race 2026-x is in progress or unfinished" in str(e.value.code)
    assert "photofinder race import 2026-x" in str(e.value.code)
    assert not (data_root / "yipai").exists()
    assert yipai.refusal("ORD2") is None


def test_download_yipai_script_refuses_order_of_an_unfinished_import(data_root, tmp_path):
    unfinished_import(data_root)
    r = subprocess.run(["bash", "scripts/download_yipai.sh", ORDER], cwd=REPO, env=shell_env(data_root, tmp_path),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1
    assert "is in progress or unfinished" in r.stderr
    assert not (data_root / "yipai").exists()
    assert not (tmp_path / "caffeinate-ran").exists()


def shell_env(root, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "caffeinate").write_text(f"#!/bin/sh\ntouch {tmp_path}/caffeinate-ran\n")
    (fake / "caffeinate").chmod(0o755)
    return {**os.environ, "PHOTOFINDER_DATA_ROOT": str(root), "PATH": f"{fake}:{os.environ['PATH']}"}


def test_download_yipai_script_refuses_registered_order(data_root, tmp_path):
    race(YIPAI_URL)
    r = subprocess.run(["bash", "scripts/download_yipai.sh", ORDER], cwd=REPO, env=shell_env(data_root, tmp_path),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1
    assert "scripts/download.sh 2026-x" in r.stderr
    assert not (data_root / "yipai").exists()
    assert not (tmp_path / "caffeinate-ran").exists()


def test_download_script_refuses_unregistered_race(data_root, tmp_path):
    race(YIPAI_URL)
    r = subprocess.run(["bash", "scripts/download.sh", "2026-typo"], cwd=REPO, env=shell_env(data_root, tmp_path),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1
    assert "2026-typo is not a registered race" in r.stderr
    assert not (data_root / "races").exists()
    assert not (tmp_path / "caffeinate-ran").exists()


@pytest.mark.parametrize("script", ["download.sh", "download_yipai.sh"])
def test_scripts_parse(script):
    assert subprocess.run(["bash", "-n", REPO / "scripts" / script]).returncode == 0
