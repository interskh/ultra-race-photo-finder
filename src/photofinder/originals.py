import csv
import io
import logging
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path

import httpx

from photofinder import config
from photofinder.index.stages import MANIFEST_NAME
from photofinder.sources import yipai

LOOKUP_GAP = 6.0
RATE_LIMIT_WAITS = (60, 120, 240)
LOOKUP_PAGE_SIZE = 100
MAX_LOOKUP_PAGES = 10
NAME_PART_MAX = 40
CSV_NAME = "photos.csv"
ORIGINALS = "originals"
DOWNLOADED = "downloaded"
COLUMNS = ["source_photo_id", "original_file_name", "photographer", "taken_at", "album", "group", "preview_path",
           "original_path", "status"]
COUNTS = ("downloaded", "skipped", "buy_on_site", "failed")
CSV_LOCK = threading.Lock()

log = logging.getLogger("originals")


class Cancelled(Exception):
    pass


class BuyOnSite(Exception):
    pass


class Failed(Exception):
    pass


class Busy(Exception):
    pass


def safe_name(name: str | None, fallback="profile") -> str:
    return re.sub(r"[^\w-]+", "_", unicodedata.normalize("NFC", name or "")).strip("_")[:NAME_PART_MAX] or fallback


def folder_key(name: str) -> str:
    return safe_name(name).casefold()


def is_yipai(collection: Path) -> bool:
    return (collection / MANIFEST_NAME).is_file()


def profile_folder(collection: Path, profile: str) -> Path:
    return config.DATA_ROOT / "exports" / collection.name / safe_name(profile)


def file_name(meta: dict) -> str:
    t = meta["taken_at"]
    stamp = t.replace("-", "").replace(":", "").replace(" ", "-") if t else "undated"
    return f"{stamp}_{safe_name(meta['photographer'], 'unknown')}_{safe_name(meta['source_photo_id'], 'none')}.jpg"


def yipai_id(meta: dict) -> int | None:
    spid = meta["source_photo_id"] or ""
    return int(spid) if spid.isdigit() else None


def rows_of(collection: Path, metas: list[dict]) -> list[dict]:
    ids = [i for i in map(yipai_id, metas) if i is not None]
    manifest = {}
    if ids and is_yipai(collection):
        uri = f"{(collection / MANIFEST_NAME).resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as m:
            manifest = {pid: (order, fname) for pid, order, fname in m.execute(
                f"select photo_id, order_id, fname from photos where photo_id in ({','.join('?' * len(ids))})", ids)}
    return [{**meta, "preview": collection / meta["relpath"], "file": file_name(meta),
             **dict(zip(("order_id", "fname"), manifest.get(yipai_id(meta), ("", ""))))} for meta in metas]


def valid(path: Path) -> bool:
    return path.is_file() and yipai.looks_like_jpeg(path.read_bytes())


def read_csv(folder: Path) -> dict:
    try:
        with open(folder / CSV_NAME, newline="", encoding="utf-8-sig") as f:
            return {r["source_photo_id"]: r["status"] for r in csv.DictReader(f)}
    except (OSError, KeyError, csv.Error, UnicodeDecodeError):
        return {}


def statuses(folder: Path, rows: list[dict]) -> dict:
    old = read_csv(folder)
    out = {}
    for r in rows:
        if (folder / ORIGINALS / r["file"]).is_file():
            out[r["photo_id"]] = DOWNLOADED
        else:
            s = old.get(r["source_photo_id"] or "")
            out[r["photo_id"]] = s if s and s != DOWNLOADED else None
    return out


def write_csv(folder: Path, rows: list[dict], updates: dict | None = None) -> Path:
    with CSV_LOCK:
        status = statuses(folder, rows) | (updates or {})
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(COLUMNS)
        for r in rows:
            original = folder / ORIGINALS / r["file"]
            w.writerow([r["source_photo_id"] or "", r["fname"], r["photographer"] or "", r["taken_at"] or "",
                        r["album"] or "", r["group"] or "", r["preview"], original if original.is_file() else "",
                        status[r["photo_id"]] or ""])
        folder.mkdir(parents=True, exist_ok=True)
        yipai.write_atomic(folder / CSV_NAME, buf.getvalue().encode("utf-8-sig"))
    return folder / CSV_NAME


def zip_folder(folder: Path) -> Path | None:
    files = sorted((folder / ORIGINALS).glob("*.jpg"))
    if not files:
        return None
    fd, tmp = tempfile.mkstemp(".zip", ".originals-", folder)
    with os.fdopen(fd, "wb") as f, zipfile.ZipFile(f, "w", zipfile.ZIP_STORED) as z:
        for p in files:
            z.write(p, f"{ORIGINALS}/{p.name}")
        if (folder / CSV_NAME).is_file():
            z.write(folder / CSV_NAME, CSV_NAME)
    return Path(tmp)


class Fetcher:
    def __init__(self, client: httpx.Client | None = None, *, sleep=None, clock=time.monotonic,
                 img_delay=(0.2, 0.6), tries=5):
        self.client = client or httpx.Client(timeout=60, follow_redirects=True)
        self.stop = threading.Event()
        self.sleep = sleep or self.stop.wait
        self.clock = clock
        self.img_delay = img_delay
        self.tries = tries
        self.last_lookup = None
        self.note = lambda message: None

    def pause(self, seconds: float):
        if seconds > 0:
            self.sleep(seconds)
        if self.stop.is_set():
            raise Cancelled

    def lookup(self, order_id: str, fname: str, photo_id: int) -> dict:
        url = f"{yipai.SITE}/api/v1/yipai/order/{order_id}/audience/photos"
        page, total = 1, 1
        while page <= total:
            if page > MAX_LOOKUP_PAGES:
                raise Failed(f"gallery search for {fname} has more than {MAX_LOOKUP_PAGES} pages")
            if self.last_lookup is not None:
                self.pause(self.last_lookup + LOOKUP_GAP - self.clock())
            params = {"tagId": "", "pwd": "", "sortType": "desc", "page": page, "pageSize": LOOKUP_PAGE_SIZE,
                      "fileName": Path(fname).stem}
            for wait in (*RATE_LIMIT_WAITS, None):
                try:
                    data = yipai.request_json(self.client, "GET", url, tries=self.tries, sleep=self.pause,
                                              params=params)
                    break
                except yipai.RetriesExhausted:
                    if wait is None:
                        raise
                    self.note(f"yipai360 is rate-limiting lookups; waiting {wait}s before {fname}")
                    self.pause(wait)
                finally:
                    self.last_lookup = self.clock()
            for p in data.get("photos") or []:
                if p["photoId"] == photo_id:
                    return p["img"]
            total = data["pagination"]["totalPage"]
            page += 1
        raise BuyOnSite("not found in the gallery")

    def fetch(self, img: dict) -> bytes:
        if not img.get("sign"):
            raise BuyOnSite("no original URL")
        self.pause(random.uniform(*self.img_delay))
        error = None
        for attempt in range(self.tries):
            domain = img["primary"] if attempt % 2 == 0 else img["failover"]
            wait = yipai.backoff_seconds(attempt)
            try:
                r = self.client.get(domain + img["path"] + img["sign"], headers=yipai.HEADERS)
            except httpx.HTTPError as e:
                error = repr(e)
            else:
                if r.status_code == 403:
                    raise BuyOnSite("HTTP 403")
                if r.status_code == 200:
                    if yipai.looks_like_jpeg(r.content):
                        return r.content
                    if r.content[:2] != b"\xff\xd8":
                        raise BuyOnSite("not a JPEG")
                    error = "truncated JPEG"
                elif r.status_code in yipai.RETRYABLE:
                    error = f"HTTP {r.status_code}"
                    wait = max(wait, yipai.retry_after(r))
                else:
                    raise Failed(f"HTTP {r.status_code}")
            if attempt < self.tries - 1:
                self.pause(wait)
        raise Failed(error)

    def original(self, row: dict, dest: Path) -> str:
        dest.unlink(missing_ok=True)
        if not row["fname"]:
            return "failed: not in the gallery manifest"
        try:
            data = self.fetch(self.lookup(row["order_id"], row["fname"], yipai_id(row)))
        except BuyOnSite as e:
            return f"buy on site: {e}"
        except Failed as e:
            return f"failed: {e}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        yipai.write_atomic(dest, data)
        return DOWNLOADED


class Job:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher
        fetcher.note = lambda message: self.update(current=message)
        self.busy = threading.Lock()
        self.lock = threading.Lock()
        self.thread = None
        self.state = {"state": "idle", "profile_id": None, "profile": None, "done": 0, "total": 0, "current": None,
                      "counts": dict.fromkeys(COUNTS, 0), "errors": [], "folder": None}

    def status(self) -> dict:
        with self.lock:
            return {**self.state, "counts": dict(self.state["counts"]), "errors": list(self.state["errors"])}

    def update(self, **kw):
        with self.lock:
            self.state.update(kw)

    def claim(self, **state):
        if not self.busy.acquire(blocking=False):
            raise Busy
        with self.lock:
            self.fetcher.stop.clear()
            self.state.update(state)

    @contextmanager
    def claimed(self):
        self.claim()
        try:
            yield
        finally:
            self.busy.release()

    def start(self, profile_id: int, profile: str, folder: Path, rows: list[dict]):
        self.update(state="running", profile_id=profile_id, profile=profile, done=0, total=len(rows), current=None,
                    counts=dict.fromkeys(COUNTS, 0), errors=[], folder=str(folder))
        self.thread = threading.Thread(target=self.run, args=(folder, rows), daemon=True, name="originals")
        self.thread.start()

    def cancel(self):
        with self.lock:
            if self.state["state"] == "running":
                self.fetcher.stop.set()

    def run(self, folder: Path, rows: list[dict]):
        state, error = "done", None
        try:
            for i, row in enumerate(rows):
                self.fetcher.pause(0)
                self.update(current=row["fname"] or row["relpath"])
                dest = folder / ORIGINALS / row["file"]
                if valid(dest):
                    key, result = "skipped", DOWNLOADED
                else:
                    result = self.fetcher.original(row, dest)
                    key = "downloaded" if result == DOWNLOADED else result.split(":")[0].replace(" ", "_")
                    write_csv(folder, rows, {row["photo_id"]: result})
                with self.lock:
                    self.state["counts"][key] += 1
                    self.state["done"] = i + 1
                    if result != DOWNLOADED:
                        self.state["errors"].append(f"{row['source_photo_id']} {row['fname']}: {result}")
        except Cancelled:
            state = "cancelled"
        except yipai.Blocked as e:
            state, error = "error", f"yipai360 API unavailable: {e}"
        except Exception as e:
            log.exception("originals job failed")
            state, error = "error", f"{type(e).__name__}: {e}"
        finally:
            try:
                write_csv(folder, rows)
            except Exception as e:
                state, error = "error", error or f"could not write {CSV_NAME}: {e}"
            with self.lock:
                self.state.update(state=state, current=None)
                if error:
                    self.state["errors"].append(error)
            self.busy.release()

    def single(self, row: dict, folder: Path, rows: list[dict]) -> tuple[str, Path]:
        dest = folder / ORIGINALS / row["file"]
        result = DOWNLOADED if valid(dest) else self.fetcher.original(row, dest)
        if any(r["photo_id"] == row["photo_id"] for r in rows):
            write_csv(folder, rows, {row["photo_id"]: result})
        return result, dest
