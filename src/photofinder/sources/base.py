import fcntl
import logging
import os
import random
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from photofinder.sources.common import (RETRYABLE, AlreadyRunning, Blocked, backoff_seconds, looks_like_jpeg,
                                        retry_after, write_atomic)

SCHEMA = """
create table if not exists catalog(
  source_id text primary key, file text, fname text,
  photographer_uid text, photographer text, group_name text,
  taken_at text, width integer, height integer,
  status text not null default 'pending', error text);
create table if not exists meta(key text primary key, value text);
"""
EXPIRED = "HTTP 403"
NO_URL = "no preview url"

log = logging.getLogger("download")


@dataclass
class CatalogRow:
    source_id: str
    fname: str | None = None
    photographer_uid: str | None = None
    photographer: str | None = None
    group_name: str | None = None
    taken_at: str | None = None
    width: int | None = None
    height: int | None = None
    url: str | None = field(default=None, compare=False, repr=False)


def jpeg_file_ok(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if size <= 1024:
                return False
            head = f.read(2)
            f.seek(-64, os.SEEK_END)
            tail = f.read()
    except OSError:
        return False
    return head == b"\xff\xd8" and b"\xff\xd9" in tail


class AlbumDownloader:
    def __init__(self, client: httpx.Client, adapter, out_dir: Path, *, concurrency=4, page_delay=3.0,
                 img_delay=(0.2, 0.6), tries=5, max_consecutive_failures=20, sleep=time.sleep,
                 clock=time.monotonic):
        self.client = client
        self.adapter = adapter
        self.out_dir = out_dir
        self.photos_dir = out_dir / "photos"
        self.concurrency = concurrency
        self.page_delay = page_delay
        self.img_delay = img_delay
        self.tries = tries
        self.max_consecutive_failures = max_consecutive_failures
        self.sleep = sleep
        self.clock = clock
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

    def save_meta(self, items: dict):
        self.db.executemany("insert or replace into meta values (?,?)",
                            [(k, None if v is None else str(v)) for k, v in items.items()])
        self.db.commit()

    def upsert(self, rows: list[CatalogRow]):
        self.db.executemany("""
            insert into catalog(source_id, fname, photographer_uid, photographer, group_name, taken_at, width, height)
            values (?,?,?,?,?,?,?,?)
            on conflict(source_id) do update set fname=excluded.fname, photographer_uid=excluded.photographer_uid,
              photographer=excluded.photographer, group_name=excluded.group_name, taken_at=excluded.taken_at,
              width=excluded.width, height=excluded.height""",
            [(r.source_id, r.fname, r.photographer_uid, r.photographer, r.group_name, r.taken_at, r.width, r.height)
             for r in rows])
        self.db.commit()

    def is_done(self, source_id: str) -> bool:
        row = self.db.execute("select status, file from catalog where source_id=?", (source_id,)).fetchone()
        return bool(row and row[0] == "done" and row[1] and jpeg_file_ok(self.out_dir / row[1]))

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

    def download(self, row: CatalogRow, relisted=False) -> tuple[str, str, str | None]:
        sid = row.source_id
        dest = self.photos_dir / f"{sid}.jpg"
        if jpeg_file_ok(dest):
            return sid, "done", None
        url = self.adapter.preview_url(row)
        if not url:
            return sid, "failed", NO_URL
        self.sleep(random.uniform(*self.img_delay))
        error = None
        for attempt in range(self.tries):
            self._wait_for_cooldown()
            if self.stop.is_set():
                return sid, "pending", error
            wait = backoff_seconds(attempt)
            try:
                r = self.client.get(url)
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
                    return sid, "done", None
                if r.status_code == 403:
                    if relisted:
                        self._record_failure()
                    return sid, "failed", EXPIRED
                error = f"HTTP {r.status_code}" if r.status_code != 200 else "not a jpeg"
                if r.status_code in RETRYABLE:
                    self._cool_down(max(wait, retry_after(r)))
                    wait = 0
                elif r.status_code != 200:
                    break
            if attempt < self.tries - 1:
                self.sleep(wait)
        self._record_failure()
        return sid, "failed", error

    def _download_batch(self, pool: ThreadPoolExecutor, rows: list[CatalogRow],
                        relisted=False) -> list[tuple[str, str, str | None]]:
        results = list(pool.map(self.download, rows, [relisted] * len(rows)))
        self.db.executemany("update catalog set status=?, error=?, file=? where source_id=?",
                            [(s, e, f"photos/{sid}.jpg" if s == "done" else None, sid) for sid, s, e in results])
        self.db.commit()
        return results

    def _todo(self, rows: list[CatalogRow]) -> list[CatalogRow]:
        return [r for r in rows if not self.is_done(r.source_id)]

    def run(self) -> dict[str, int]:
        if self._lockfile is None:
            self.acquire_lock()
        meta = self.adapter.meta()
        expected = meta.get("total")
        self.save_meta({"title": meta.get("title"), **getattr(self.adapter, "meta_items", dict)()})
        cursor, page = None, 1
        started = self.clock()
        with ThreadPoolExecutor(self.concurrency) as pool:
            while True:
                rows, next_cursor, total = self.adapter.list_page(cursor)
                expected = total if total is not None else expected
                self.upsert(rows)
                todo = self._todo(rows)
                results = self._download_batch(pool, todo)
                if any(e == EXPIRED for _, _, e in results) and not self.stop.is_set():
                    log.info("page %d: signed URLs rejected, re-listing once", page)
                    rows, next_cursor, total = self.adapter.list_page(cursor)
                    expected = total if total is not None else expected
                    self.upsert(rows)
                    self._download_batch(pool, self._todo(rows), relisted=True)
                done = self.db.execute("select count(*) from catalog where status='done'").fetchone()[0]
                log.info("page %d: fetched %d, done %d/%s, %.0f min elapsed", page, len(todo), done,
                         "?" if expected is None else expected, (self.clock() - started) / 60)
                if self.stop.is_set():
                    raise Blocked(f"{self.max_consecutive_failures} consecutive image failures; stopping to avoid a ban")
                if next_cursor is None:
                    break
                if next_cursor == cursor:
                    log.warning("page %d: adapter returned the same cursor %r again; ending the listing", page, cursor)
                    break
                cursor, page = next_cursor, page + 1
                self.sleep(self.page_delay + random.uniform(0, self.page_delay))
        counts = dict(self.db.execute("select status, count(*) from catalog group by status").fetchall())
        seen = sum(counts.values())
        if expected is not None and seen < expected:
            log.warning("album reports %d photos but only %d were listed (uploads/deletions during run); rerun to top up",
                        expected, seen)
            counts["missing"] = expected - seen
        return counts
