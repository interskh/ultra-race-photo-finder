import argparse
import fcntl
import logging
import random
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from photofinder import config, races
from photofinder.sources.common import (RETRYABLE, AlreadyRunning, Blocked, RetriesExhausted, backoff_seconds,
                                        looks_like_jpeg, retry_after, write_atomic)

SITE = "https://www.yipai360.com"
HEADERS = {
    "appaccess": "yipai",
    "appname": "yipai",
    "referer": f"{SITE}/photolivepc/",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
}
SIZE_KEY = "s1920"

CATALOG_SELECT = """select cast(p.photo_id as text) as source_id, p.file, p.fname, p.uid as photographer_uid,
  g.nickname as photographer, t.name as group_name, null as taken_at, p.width, p.height, p.status, p.error
  from photos p left join photographers g on g.uid = p.uid left join tags t on t.tag_id = p.tag_id"""
SCHEMA = f"""
create table if not exists photos(
  photo_id integer primary key, order_id text, tag_id integer, uid text, fname text,
  width integer, height integer, size integer, create_time integer, path text,
  file text, status text not null default 'pending', error text);
create table if not exists tags(tag_id integer primary key, order_id text, name text);
create table if not exists photographers(uid text primary key, nickname text);
create view if not exists catalog as {CATALOG_SELECT};
"""

log = logging.getLogger("yipai")


def request_json(client: httpx.Client, method: str, url: str, *, tries=5, sleep=time.sleep, **kw) -> dict:
    for attempt in range(tries):
        wait = backoff_seconds(attempt)
        try:
            r = client.request(method, url, headers=HEADERS, **kw)
            if r.status_code == 200:
                body = r.json()
                if body.get("status") == 200:
                    return body["data"]
                log.warning("api %s returned status=%s message=%r", url, body.get("status"), body.get("message"))
            elif r.status_code not in RETRYABLE:
                raise Blocked(f"api {url} -> HTTP {r.status_code}")
            else:
                wait = max(wait, retry_after(r))
                log.warning("api %s -> HTTP %s (attempt %d)", url, r.status_code, attempt + 1)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("api %s error %r (attempt %d)", url, e, attempt + 1)
        if attempt < tries - 1:
            sleep(wait)
    raise RetriesExhausted(f"api {url} failed after {tries} attempts")


class Downloader:
    def __init__(self, client: httpx.Client, order_id: str, out_dir: Path, *, concurrency=3,
                 page_size=500, page_delay=3.0, img_delay=(0.2, 0.6), tries=5,
                 max_consecutive_failures=20, sleep=time.sleep, clock=time.monotonic):
        self.client = client
        self.order_id = order_id
        self.out_dir = out_dir
        self.photos_dir = out_dir / "photos"
        self.concurrency = concurrency
        self.page_size = page_size
        self.page_delay = page_delay
        self.img_delay = img_delay
        self.tries = tries
        self.max_consecutive_failures = max_consecutive_failures
        self.sleep = sleep
        self.clock = clock
        self.api = f"{SITE}/api/v1/yipai/order/{order_id}"
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._cooldown_until = 0.0
        self._lockfile = None
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(out_dir / "manifest.sqlite")
        self.db.executescript(SCHEMA)

    def acquire_lock(self):
        f = open(self.out_dir / ".download.lock", "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise AlreadyRunning(f"another download is already running for {self.out_dir}")
        self._lockfile = f

    def close(self):
        self.db.close()
        if self._lockfile is not None:
            self._lockfile.close()
            self._lockfile = None

    def _request_json(self, method: str, url: str, **kw) -> dict:
        return request_json(self.client, method, url, tries=self.tries, sleep=self.sleep, **kw)

    def list_page(self, page: int) -> tuple[dict, list[dict]]:
        data = self._request_json("GET", f"{self.api}/audience/photos", params={
            "tagId": "", "sortType": "asc", "page": page, "pageSize": self.page_size})
        return data["pagination"], data["photos"]

    def save_order_meta(self):
        tags = self._request_json("POST", f"{SITE}/applet/v2/order/get-tag-list", data={"orderId": self.order_id})
        self.db.executemany("insert or replace into tags values (?,?,?)",
                            [(t["id"], self.order_id, t["name"]) for t in tags])
        people = self._request_json("GET", f"{self.api}/setting/audience/auth/photographers")["photographers"]
        self.db.executemany("insert or replace into photographers values (?,?)",
                            [(p["uid"], p["nickname"]) for p in people])
        self.db.commit()

    def upsert(self, photos: list[dict]):
        self.db.executemany("""
            insert into photos(photo_id, order_id, tag_id, uid, fname, width, height, size, create_time, path)
            values (?,?,?,?,?,?,?,?,?,?)
            on conflict(photo_id) do update set tag_id=excluded.tag_id, uid=excluded.uid,
              fname=excluded.fname, width=excluded.width, height=excluded.height,
              size=excluded.size, create_time=excluded.create_time, path=excluded.path""",
            [(p["photoId"], p["orderId"], p["tagId"], p["uid"], p["fname"], int(p["width"]),
              int(p["height"]), int(p["size"]), int(p["createTime"]), p["img"]["path"]) for p in photos])
        self.db.commit()

    def is_done(self, photo_id: int) -> bool:
        row = self.db.execute("select status, file from photos where photo_id=?", (photo_id,)).fetchone()
        return bool(row and row[0] == "done" and (self.out_dir / row[1]).is_file())

    def _record_failure(self):
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.max_consecutive_failures:
                self.stop.set()

    def _record_success(self):
        with self._lock:
            self._consecutive_failures = 0

    def _cool_down(self, seconds: float):
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self.clock() + seconds)

    def _wait_for_cooldown(self):
        while not self.stop.is_set():
            with self._lock:
                wait = self._cooldown_until - self.clock()
            if wait <= 0:
                return
            self.sleep(min(wait, 5))

    def download(self, photo: dict) -> tuple[int, str, str | None]:
        pid = photo["photoId"]
        dest = self.photos_dir / f"{pid}.jpg"
        if dest.is_file() and looks_like_jpeg(dest.read_bytes()):
            return pid, "done", None
        img = photo["img"]
        if SIZE_KEY not in img:
            return pid, "failed", f"no {SIZE_KEY} url"
        self.sleep(random.uniform(*self.img_delay))
        error = None
        for attempt in range(self.tries):
            self._wait_for_cooldown()
            if self.stop.is_set():
                return pid, "pending", error
            domain = img["primary"] if attempt % 2 == 0 else img["failover"]
            wait = backoff_seconds(attempt)
            try:
                r = self.client.get(domain + img["path"] + img[SIZE_KEY], headers=HEADERS)
            except httpx.HTTPError as e:
                error = repr(e)
            else:
                if r.status_code == 200 and looks_like_jpeg(r.content):
                    try:
                        write_atomic(dest, r.content)
                    except OSError:
                        self.stop.set()
                        raise
                    self._record_success()
                    return pid, "done", None
                if r.status_code == 403:
                    self._record_failure()
                    return pid, "expired", "HTTP 403"
                error = f"HTTP {r.status_code}" if r.status_code != 200 else "not a jpeg"
                if r.status_code in RETRYABLE:
                    self._cool_down(max(wait, retry_after(r)))
                    wait = 0
                elif r.status_code != 200:
                    break
            if attempt < self.tries - 1:
                self.sleep(wait)
        self._record_failure()
        return pid, "failed", error

    def _download_batch(self, pool: ThreadPoolExecutor, photos: list[dict]) -> list[tuple[int, str, str | None]]:
        results = list(pool.map(self.download, photos))
        self.db.executemany("update photos set status=?, error=?, file=? where photo_id=?",
                            [(s, e, f"photos/{pid}.jpg" if s == "done" else None, pid) for pid, s, e in results])
        self.db.commit()
        return results

    def _todo(self, photos: list[dict]) -> list[dict]:
        return [p for p in photos if not self.is_done(p["photoId"])]

    def run(self) -> dict[str, int]:
        if self._lockfile is None:
            self.acquire_lock()
        self.save_order_meta()
        page, total_pages, expected = 1, 1, 0
        started = time.monotonic()
        with ThreadPoolExecutor(self.concurrency) as pool:
            while page <= total_pages:
                pagination, photos = self.list_page(page)
                total_pages, expected = pagination["totalPage"], pagination["count"]
                self.upsert(photos)
                todo = self._todo(photos)
                results = self._download_batch(pool, todo)
                if any(s == "expired" for _, s, _ in results) and not self.stop.is_set():
                    log.info("page %d: signed URLs rejected, re-listing once", page)
                    pagination, fresh = self.list_page(page)
                    total_pages, expected = pagination["totalPage"], pagination["count"]
                    self.upsert(fresh)
                    self._download_batch(pool, self._todo(fresh))
                done = self.db.execute("select count(*) from photos where status='done'").fetchone()[0]
                log.info("page %d/%d: fetched %d, done %d/%d, %.0f min elapsed", page, total_pages,
                         len(todo), done, expected, (time.monotonic() - started) / 60)
                if self.stop.is_set():
                    raise Blocked(f"{self.max_consecutive_failures} consecutive image failures; stopping to avoid a ban")
                page += 1
                if page <= total_pages:
                    self.sleep(self.page_delay + random.uniform(0, self.page_delay))
        counts = dict(self.db.execute("select status, count(*) from photos group by status").fetchall())
        seen = sum(counts.values())
        if seen < expected:
            log.warning("gallery reports %d photos but only %d were listed (uploads/deletions during run); rerun to top up",
                        expected, seen)
            counts["missing"] = expected - seen
        return counts


def site_link(site_id: str, fname: str | None) -> dict:
    return {"url": f"{SITE}/photolivepc/?orderId={site_id}", "exact": False, "find_by": fname,
            "hint": "search the full file name (with extension) in 通过照片名搜索 and press Enter"}


def refusal(order_id: str) -> str | None:
    if owner := races.load().owner(f"yipai-{order_id}"):
        return (f"yipai order {order_id} belongs to race {owner.slug}; "
                f"download it with: scripts/download.sh {owner.slug}")
    if album := next((config.DATA_ROOT / "races").glob(f"*/albums/yipai-{order_id}"), None):
        slug = album.parent.parent.name
        return (f"yipai order {order_id}: an import into race {slug} is in progress or unfinished ({album}); "
                f"rerun `photofinder race import {slug} …` to finish it")
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Download all watermarked 1920px previews of a yipai360 gallery")
    ap.add_argument("order_id")
    ap.add_argument("--data-root", type=Path, default=config.DATA_ROOT)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--page-delay", type=float, default=3.0)
    args = ap.parse_args(argv)

    if not args.data_root.parent.is_dir():
        sys.exit(f"{args.data_root.parent} does not exist; is its disk mounted?")
    if args.data_root.resolve() == config.DATA_ROOT.resolve() and (msg := refusal(args.order_id)):
        sys.exit(msg)
    out_dir = args.data_root / "yipai" / args.order_id
    out_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(out_dir / "download.log")])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        dl = Downloader(client, args.order_id, out_dir, concurrency=args.concurrency, page_delay=args.page_delay)
        try:
            counts = dl.run()
        except AlreadyRunning as e:
            sys.exit(str(e))
        except Blocked as e:
            log.error("stopped: %s (rerun later to resume)", e)
            sys.exit(2)
    log.info("finished: %s", counts)
    if set(counts) - {"done"}:
        sys.exit(1)


if __name__ == "__main__":
    main()
