import sqlite3
from collections import Counter

import httpx
import pytest

from photofinder.sources.yipai import Blocked, Downloader

ORDER = "ORD1"
JPEG = b"\xff\xd8" + b"x" * 2000 + b"\xff\xd9"


def photo(pid, sign="s"):
    return {"photoId": pid, "orderId": ORDER, "tagId": 7, "uid": "u1", "fname": "same-name.jpg",
            "width": "1920", "height": "1280", "size": "100", "createTime": str(1000 + pid),
            "img": {"primary": "https://cdn-a.test", "failover": "https://cdn-b.test",
                    "path": f"/p/{pid}", "s1920": f"?sig={sign}"}}


class FakeSite:
    def __init__(self, n_photos=5, page_size=2):
        self.photos = [photo(i) for i in range(1, n_photos + 1)]
        self.page_size = page_size
        self.hits = Counter()
        self.image_handler = lambda req: httpx.Response(200, content=JPEG)

    def __call__(self, req: httpx.Request):
        url = req.url
        if url.path.endswith("/audience/photos"):
            self.hits["list"] += 1
            page = int(url.params["page"])
            chunk = self.photos[(page - 1) * self.page_size: page * self.page_size]
            total = -(-len(self.photos) // self.page_size)
            return httpx.Response(200, json={"status": 200, "data": {
                "pagination": {"page": page, "count": len(self.photos), "totalPage": total},
                "photos": chunk}})
        if url.path.endswith("get-tag-list"):
            return httpx.Response(200, json={"status": 200, "data": [{"id": 7, "name": "9.26 race"}]})
        if url.path.endswith("photographers"):
            return httpx.Response(200, json={"status": 200, "data": {"photographers": [{"uid": "u1", "nickname": "cam"}]}})
        self.hits[f"img:{url.host}"] += 1
        return self.image_handler(req)


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, s):
        self.slept.append(s)
        self.now += s

    def clock(self):
        return self.now


def make(tmp_path, site, fake_time=None, **kw):
    t = fake_time or FakeTime()
    client = httpx.Client(transport=httpx.MockTransport(site))
    return Downloader(client, ORDER, tmp_path, page_size=site.page_size, sleep=t.sleep, clock=t.clock, **kw)


def statuses(tmp_path):
    db = sqlite3.connect(tmp_path / "manifest.sqlite")
    return dict(db.execute("select photo_id, status from photos").fetchall())


def test_downloads_every_page_named_by_photo_id(tmp_path):
    site = FakeSite(n_photos=5, page_size=2)
    counts = make(tmp_path, site).run()
    assert counts == {"done": 5}
    assert sorted(p.name for p in (tmp_path / "photos").iterdir()) == [f"{i}.jpg" for i in range(1, 6)]
    db = sqlite3.connect(tmp_path / "manifest.sqlite")
    assert db.execute("select name from tags").fetchall() == [("9.26 race",)]
    assert db.execute("select nickname from photographers where uid='u1'").fetchone() == ("cam",)
    assert db.execute("select uid, create_time from photos where photo_id=3").fetchone() == ("u1", 1003)


def test_rerun_skips_already_downloaded(tmp_path):
    site = FakeSite(n_photos=3)
    make(tmp_path, site).run()
    first = sum(v for k, v in site.hits.items() if k.startswith("img:"))
    make(tmp_path, site).run()
    assert sum(v for k, v in site.hits.items() if k.startswith("img:")) == first == 3


def test_rerun_refetches_if_file_was_deleted(tmp_path):
    site = FakeSite(n_photos=2)
    make(tmp_path, site).run()
    (tmp_path / "photos" / "1.jpg").unlink()
    assert make(tmp_path, site).run() == {"done": 2}
    assert (tmp_path / "photos" / "1.jpg").read_bytes() == JPEG


def test_file_written_before_a_crash_is_not_refetched(tmp_path):
    site = FakeSite(n_photos=2)
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "1.jpg").write_bytes(JPEG)
    assert make(tmp_path, site).run() == {"done": 2}
    assert sum(v for k, v in site.hits.items() if k.startswith("img:")) == 1


def test_server_error_retries_on_failover_domain(tmp_path):
    site = FakeSite(n_photos=1)
    site.image_handler = lambda req: httpx.Response(503) if req.url.host == "cdn-a.test" else httpx.Response(200, content=JPEG)
    assert make(tmp_path, site).run() == {"done": 1}
    assert site.hits["img:cdn-b.test"] == 1


def test_truncated_image_is_not_saved(tmp_path):
    site = FakeSite(n_photos=1)
    site.image_handler = lambda req: httpx.Response(200, content=b"\xff\xd8" + b"x" * 2000)
    assert make(tmp_path, site, tries=2, max_consecutive_failures=99).run() == {"failed": 1}
    assert list((tmp_path / "photos").iterdir()) == []


def test_expired_signature_relists_page_and_uses_fresh_url(tmp_path):
    site = FakeSite(n_photos=1)
    site.image_handler = lambda req: httpx.Response(200, content=JPEG) if req.url.params["sig"] == "fresh" else httpx.Response(403)
    original_call = site.__call__

    def relist_gives_fresh(req):
        resp = original_call(req)
        if req.url.path.endswith("/audience/photos") and site.hits["list"] >= 2:
            site.photos = [photo(1, sign="fresh")]
            resp = original_call(req)
        return resp

    client = httpx.Client(transport=httpx.MockTransport(relist_gives_fresh))
    dl = Downloader(client, ORDER, tmp_path, page_size=2, sleep=lambda s: None)
    assert dl.run() == {"done": 1}


def test_persistent_failures_stop_the_run(tmp_path):
    site = FakeSite(n_photos=10, page_size=5)
    site.image_handler = lambda req: httpx.Response(503)
    with pytest.raises(Blocked):
        make(tmp_path, site, tries=2, max_consecutive_failures=3, concurrency=1).run()
    assert site.hits["list"] == 1
    assert Counter(statuses(tmp_path).values())["failed"] == 3


def test_api_hard_error_raises_blocked(tmp_path):
    def handler(req):
        return httpx.Response(401)
    dl = Downloader(httpx.Client(transport=httpx.MockTransport(handler)), ORDER, tmp_path, sleep=lambda s: None)
    with pytest.raises(Blocked):
        dl.run()


def test_persistent_403_across_pages_trips_breaker(tmp_path):
    site = FakeSite(n_photos=20, page_size=5)
    site.image_handler = lambda req: httpx.Response(403)
    with pytest.raises(Blocked):
        make(tmp_path, site, max_consecutive_failures=6, concurrency=1).run()
    assert sum(v for k, v in site.hits.items() if k.startswith("img:")) <= 7


def test_429_retry_after_pauses_before_next_request(tmp_path):
    site = FakeSite(n_photos=1)
    replies = iter([httpx.Response(429, headers={"retry-after": "120"}), httpx.Response(200, content=JPEG)])
    site.image_handler = lambda req: next(replies)
    t = FakeTime()
    assert make(tmp_path, site, fake_time=t).run() == {"done": 1}
    assert site.hits["img:cdn-b.test"] == 1
    assert t.now >= 119


def test_http_date_retry_after_is_honored(tmp_path):
    from email.utils import formatdate
    import time
    site = FakeSite(n_photos=1)
    replies = iter([httpx.Response(503, headers={"retry-after": formatdate(time.time() + 300, usegmt=True)}),
                    httpx.Response(200, content=JPEG)])
    site.image_handler = lambda req: next(replies)
    t = FakeTime()
    assert make(tmp_path, site, fake_time=t).run() == {"done": 1}
    assert t.now >= 290


def test_waiting_worker_honors_cooldown_extended_by_another(tmp_path):
    t = FakeTime()
    dl = make(tmp_path, FakeSite(n_photos=1), fake_time=t)
    dl._cool_down(120)
    original_sleep = dl.sleep

    def extend_once(s):
        original_sleep(s)
        if t.now >= 60 and dl._cooldown_until < 300:
            dl._cool_down(300 - t.now)
    dl.sleep = extend_once
    dl._wait_for_cooldown()
    assert t.now >= 300


def test_image_requests_look_like_a_browser(tmp_path):
    site = FakeSite(n_photos=1)
    seen = []
    site.image_handler = lambda req: seen.append(req.headers["user-agent"]) or httpx.Response(200, content=JPEG)
    make(tmp_path, site).run()
    assert seen and "Mozilla" in seen[0]


def test_second_instance_on_same_folder_is_refused(tmp_path):
    from photofinder.sources.yipai import AlreadyRunning
    site = FakeSite(n_photos=1)
    first = make(tmp_path, site)
    first.acquire_lock()
    with pytest.raises(AlreadyRunning):
        make(tmp_path, site).run()


def test_relist_after_403_also_downloads_newly_uploaded_photos(tmp_path):
    site = FakeSite(n_photos=1)
    site.image_handler = lambda req: httpx.Response(200, content=JPEG) if req.url.params["sig"] == "fresh" else httpx.Response(403)
    original_call = site.__call__

    def relist_adds_upload(req):
        if req.url.path.endswith("/audience/photos") and site.hits["list"] >= 1:
            site.photos = [photo(1, sign="fresh"), photo(2, sign="fresh")]
        return original_call(req)

    client = httpx.Client(transport=httpx.MockTransport(relist_adds_upload))
    assert Downloader(client, ORDER, tmp_path, page_size=2, sleep=lambda s: None).run() == {"done": 2}


def test_gallery_count_larger_than_listed_is_reported_missing(tmp_path):
    site = FakeSite(n_photos=2)
    original_call = site.__call__

    def lying_count(req):
        resp = original_call(req)
        if req.url.path.endswith("/audience/photos"):
            body = resp.json()
            body["data"]["pagination"]["count"] = 5
            return httpx.Response(200, json=body)
        return resp

    client = httpx.Client(transport=httpx.MockTransport(lying_count))
    assert Downloader(client, ORDER, tmp_path, page_size=2, sleep=lambda s: None).run() == {"done": 2, "missing": 3}


def test_truncated_file_left_on_disk_is_redownloaded(tmp_path):
    site = FakeSite(n_photos=1)
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "1.jpg").write_bytes(b"\xff\xd8" + b"x" * 2000)
    assert make(tmp_path, site).run() == {"done": 1}
    assert (tmp_path / "photos" / "1.jpg").read_bytes() == JPEG
