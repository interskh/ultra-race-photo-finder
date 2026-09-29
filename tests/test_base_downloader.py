import logging
import re
import sqlite3
import threading
from collections import Counter

import httpx
import pytest

from photofinder.sources import common, yipai
from photofinder.sources.base import AlbumDownloader, CatalogRow

JPEG = b"\xff\xd8" + b"x" * 2000 + b"\xff\xd9"


class FakeAdapter:
    def __init__(self, pages, total=None, photographer="cam"):
        self.pages = pages
        self.total = sum(map(len, pages)) if total is None else total
        self.photographer = photographer
        self.lists = Counter()
        self.events = []

    def meta(self):
        return {"title": "Glacier 100", "total": self.total}

    def meta_items(self):
        return {"album_id": "13800138000"}

    def list_page(self, cursor):
        i = cursor or 0
        self.lists[i] += 1
        self.events.append(("list", i))
        rows = [CatalogRow(sid, fname=f"IMG_{sid}.JPG", photographer_uid="u1", photographer=self.photographer,
                           group_name="finish", taken_at="2026-09-26 08:00:00", width=1600, height=1067,
                           url=f"https://img.test/{sid}?v={self.lists[i]}") for sid in self.pages[i]]
        return rows, (i + 1 if i + 1 < len(self.pages) else None), self.total

    def preview_url(self, row):
        return row.url


class Images:
    def __init__(self):
        self.hits = Counter()
        self._lock = threading.Lock()
        self.handler = lambda req: httpx.Response(200, content=JPEG)

    def __call__(self, req):
        with self._lock:
            self.hits[req.url.path.strip("/")] += 1
        return self.handler(req)


class FakeTime:
    def __init__(self, events):
        self.now = 0.0
        self.events = events

    def sleep(self, s):
        self.events.append(("sleep", s))
        self.now += s

    def clock(self):
        return self.now


def make(tmp_path, adapter, images, **kw):
    t = FakeTime(adapter.events)
    client = httpx.Client(transport=httpx.MockTransport(images))
    return AlbumDownloader(client, adapter, tmp_path, sleep=t.sleep, clock=t.clock, **kw)


def run(tmp_path, adapter, images, **kw):
    d = make(tmp_path, adapter, images, **kw)
    try:
        return d.run()
    finally:
        d.close()


def catalog(tmp_path, cols="status, file, error"):
    db = sqlite3.connect(tmp_path / "manifest.sqlite")
    return {sid: rest for sid, *rest in db.execute(f"select source_id, {cols} from catalog").fetchall()}


def test_helpers_are_the_same_objects_in_yipai():
    for name in ["looks_like_jpeg", "write_atomic", "backoff_seconds", "retry_after", "RETRYABLE",
                 "Blocked", "RetriesExhausted", "AlreadyRunning"]:
        assert getattr(yipai, name) is getattr(common, name)
    with pytest.raises(yipai.Blocked):
        raise common.RetriesExhausted("x")


def test_downloads_every_page_into_catalog_and_meta(tmp_path):
    adapter, images = FakeAdapter([["a1", "a2"], ["b1", "b2"], ["c1"]]), Images()
    assert run(tmp_path, adapter, images) == {"done": 5}
    assert sorted(p.name for p in (tmp_path / "photos").iterdir()) == ["a1.jpg", "a2.jpg", "b1.jpg", "b2.jpg", "c1.jpg"]
    assert catalog(tmp_path)["b2"] == ["done", "photos/b2.jpg", None]
    assert catalog(tmp_path, "fname, photographer_uid, photographer, group_name, taken_at, width, height")["c1"] == [
        "IMG_c1.JPG", "u1", "cam", "finish", "2026-09-26 08:00:00", 1600, 1067]
    db = sqlite3.connect(tmp_path / "manifest.sqlite")
    assert dict(db.execute("select key, value from meta")) == {"title": "Glacier 100", "album_id": "13800138000"}


def test_schema_is_the_shared_catalog_contract(tmp_path):
    make(tmp_path, FakeAdapter([[]]), Images()).close()
    db = sqlite3.connect(tmp_path / "manifest.sqlite")
    assert [r[1:6] for r in db.execute("pragma table_info(catalog)")] == [
        ("source_id", "TEXT", 0, None, 1), ("file", "TEXT", 0, None, 0), ("fname", "TEXT", 0, None, 0),
        ("photographer_uid", "TEXT", 0, None, 0), ("photographer", "TEXT", 0, None, 0),
        ("group_name", "TEXT", 0, None, 0), ("taken_at", "TEXT", 0, None, 0),
        ("width", "INTEGER", 0, None, 0), ("height", "INTEGER", 0, None, 0),
        ("status", "TEXT", 1, "'pending'", 0), ("error", "TEXT", 0, None, 0)]
    assert [r[1:6] for r in db.execute("pragma table_info(meta)")] == [
        ("key", "TEXT", 0, None, 1), ("value", "TEXT", 0, None, 0)]


def test_page_delay_between_pages_only_and_image_delay_per_photo(tmp_path):
    adapter, images = FakeAdapter([["a1", "a2"], ["b1", "b2"], ["c1"]]), Images()
    run(tmp_path, adapter, images, page_delay=10.0, img_delay=(0.2, 0.6))
    kinds = ["page" if k == "sleep" and v >= 10 else k for k, v in adapter.events]
    assert [k for k in kinds if k != "sleep"] == ["list", "page", "list", "page", "list"]
    page_sleeps = [s for k, s in adapter.events if k == "sleep" and s >= 10]
    assert all(10 <= s <= 20 for s in page_sleeps)
    img_sleeps = [s for k, s in adapter.events if k == "sleep" and s < 10]
    assert len(img_sleeps) == 5 and all(0.2 <= s <= 0.6 for s in img_sleeps)


def test_rerun_downloads_nothing_and_refetches_missing_or_corrupt_files(tmp_path):
    adapter, images = FakeAdapter([["a1", "a2"], ["b1", "b2"]]), Images()
    run(tmp_path, adapter, images)
    assert sum(images.hits.values()) == 4
    assert run(tmp_path, adapter, images) == {"done": 4}
    assert sum(images.hits.values()) == 4
    (tmp_path / "photos" / "a1.jpg").unlink()
    (tmp_path / "photos" / "a2.jpg").write_bytes(JPEG[:1500])
    (tmp_path / "photos" / "b1.jpg").write_bytes(b"\xff\xd8" + b"x" * 500 + b"\xff\xd9")
    assert run(tmp_path, adapter, images) == {"done": 4}
    assert images.hits == {"a1": 2, "a2": 2, "b1": 2, "b2": 1}
    assert (tmp_path / "photos" / "a2.jpg").read_bytes() == JPEG


def test_pending_row_with_a_valid_file_is_marked_done_without_fetching(tmp_path):
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "a1.jpg").write_bytes(JPEG)
    adapter, images = FakeAdapter([["a1", "a2"]]), Images()
    assert run(tmp_path, adapter, images) == {"done": 2}
    assert images.hits == {"a2": 1}


def test_upsert_updates_metadata_but_keeps_done_rows_done(tmp_path):
    images = Images()
    run(tmp_path, FakeAdapter([["a1", "a2"]]), images)
    assert run(tmp_path, FakeAdapter([["a1", "a2", "a3"]], photographer="renamed"), images) == {"done": 3}
    assert catalog(tmp_path, "status, file, photographer")["a1"] == ["done", "photos/a1.jpg", "renamed"]
    assert images.hits == {"a1": 1, "a2": 1, "a3": 1}
    d = make(tmp_path, FakeAdapter([[]]), images)
    d.upsert([CatalogRow("a1", photographer="again")])
    d.close()
    assert catalog(tmp_path, "status, file, photographer")["a1"] == ["done", "photos/a1.jpg", "again"]


def test_403_relists_the_page_once_for_fresh_urls(tmp_path):
    adapter, images = FakeAdapter([["a1", "a2"], ["b1"]]), Images()
    images.handler = lambda req: httpx.Response(403 if req.url.params["v"] == "1" and req.url.path == "/a2" else 200,
                                                content=JPEG)
    assert run(tmp_path, adapter, images) == {"done": 3}
    assert adapter.lists == {0: 2, 1: 1}
    assert images.hits == {"a1": 1, "a2": 2, "b1": 1}


def test_second_403_is_not_relisted_again(tmp_path):
    adapter, images = FakeAdapter([["a1", "a2"]]), Images()
    images.handler = lambda req: httpx.Response(403)
    assert run(tmp_path, adapter, images) == {"failed": 2}
    assert adapter.lists == {0: 2}
    assert catalog(tmp_path)["a1"] == ["failed", None, "HTTP 403"]


def test_a_whole_page_of_expired_urls_is_relisted_not_breaker_tripped(tmp_path):
    adapter, images = FakeAdapter([[f"a{i}" for i in range(30)], ["b1"]]), Images()
    images.handler = lambda req: httpx.Response(403 if req.url.params["v"] == "1" else 200, content=JPEG)
    assert run(tmp_path, adapter, images) == {"done": 31}
    assert adapter.lists == {0: 2, 1: 2}


def test_relisted_page_still_403_trips_the_breaker(tmp_path):
    adapter, images = FakeAdapter([[f"a{i}" for i in range(30)], ["b1"]]), Images()
    images.handler = lambda req: httpx.Response(403)
    with pytest.raises(common.Blocked, match="20 consecutive"):
        run(tmp_path, adapter, images, concurrency=1)
    assert adapter.lists == {0: 2}
    assert Counter(s for s, *_ in catalog(tmp_path).values()) == {"failed": 20, "pending": 10}


def test_adapter_repeating_its_cursor_ends_the_listing(tmp_path, caplog):
    class Echo(FakeAdapter):
        def list_page(self, cursor):
            if self.lists[0] > 5:
                raise RuntimeError("paging never ends")
            rows, _, total = super().list_page(cursor)
            return rows, 0, total

    adapter = Echo([["a1", "a2"]])
    caplog.set_level(logging.WARNING)
    assert run(tmp_path, adapter, Images()) == {"done": 2}
    assert adapter.lists == {0: 2}
    assert "same cursor" in caplog.text


def test_breaker_stops_after_consecutive_failures_and_lists_no_more_pages(tmp_path):
    adapter, images = FakeAdapter([[f"a{i}" for i in range(10)], [f"b{i}" for i in range(10)], ["c1"]]), Images()
    images.handler = lambda req: httpx.Response(404)
    with pytest.raises(common.Blocked, match="20 consecutive"):
        run(tmp_path, adapter, images)
    assert adapter.lists == {0: 1, 1: 1}
    assert set(s for s, *_ in catalog(tmp_path).values()) == {"failed"}


def test_breaker_leaves_unattempted_photos_pending(tmp_path):
    adapter, images = FakeAdapter([[f"a{i}" for i in range(30)]]), Images()
    images.handler = lambda req: httpx.Response(404)
    with pytest.raises(common.Blocked):
        run(tmp_path, adapter, images, concurrency=1)
    assert Counter(s for s, *_ in catalog(tmp_path).values()) == {"failed": 20, "pending": 10}


def test_listed_fewer_than_reported_total_warns_with_missing_count(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    assert run(tmp_path, FakeAdapter([["a1", "a2"]], total=5), Images()) == {"done": 2, "missing": 3}
    assert "reports 5 photos but only 2 were listed" in caplog.text


def test_unknown_total_gives_no_missing_count(tmp_path):
    adapter = FakeAdapter([["a1"]])
    adapter.total = None
    assert run(tmp_path, adapter, Images()) == {"done": 1}


def test_second_downloader_on_the_same_album_is_refused_until_close(tmp_path):
    first = make(tmp_path, FakeAdapter([["a1"]]), Images())
    first.acquire_lock()
    second = make(tmp_path, FakeAdapter([["a1"]]), Images())
    with pytest.raises(common.AlreadyRunning, match=re.escape(str(tmp_path))):
        second.run()
    first.close()
    second.acquire_lock()
    second.close()


def test_fetch_json_retries_then_checks_body_and_gives_up(tmp_path):
    replies = iter([httpx.Response(503), httpx.Response(200, json={"Code": 8}), httpx.Response(200, json={"Code": 0, "Data": [1]})])
    client = httpx.Client(transport=httpx.MockTransport(lambda req: next(replies)))

    def check(body):
        if body["Code"] != 0:
            raise ValueError(f"code {body['Code']}")
        return body["Data"]

    slept = []
    assert common.fetch_json(client, "POST", "https://api.test/x", check=check, sleep=slept.append) == [1]
    assert len(slept) == 2
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    with pytest.raises(common.RetriesExhausted):
        common.fetch_json(client, "GET", "https://api.test/x", tries=3, sleep=slept.append)
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(404)))
    with pytest.raises(common.Blocked, match="HTTP 404"):
        common.fetch_json(client, "GET", "https://api.test/x", sleep=slept.append)
