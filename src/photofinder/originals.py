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
from urllib.parse import urlsplit

import httpx

from photofinder import config, races, sources
from photofinder.index.stages import ALBUMS, MANIFEST_NAME
from photofinder.sources import pailixiang, photoplus, xxpie, yipai
from photofinder.sources.common import NotFound

LOOKUP_GAP = 6.0
RATE_LIMIT_WAITS = (60, 120, 240)
LOOKUP_PAGE_SIZE = 100
MAX_LOOKUP_PAGES = 10
NAME_PART_MAX = 40
CSV_NAME = "photos.csv"
ORIGINALS = "originals"
DOWNLOADED = "downloaded"
COLUMNS = ["source_photo_id", "original_file_name", "photographer", "taken_at", "album", "group", "preview_path",
           "original_path", "status", "site_url"]
OPEN_ON_SITE = "open on site"
ORIGINAL_PLATFORMS = ("yipai", "photoplus", "pailixiang", "xxpie")
PREFIXED = ("photoplus", "pailixiang", "xxpie")
DECLARED = ("photoplus", "xxpie")
PLATFORM_NAMES = {"yipai": "yipai360", "photoplus": "photoplus", "pailixiang": "pailixiang", "xxpie": "xxpie"}
COUNTS = ("downloaded", "skipped", "buy_on_site", "failed", "open_on_site")
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


class Forbidden(BuyOnSite):
    pass


class Unavailable(Exception):
    def __init__(self, message: str, platform: str | None = None):
        super().__init__(message)
        self.platform = platform


def safe_name(name: str | None, fallback="profile") -> str:
    return re.sub(r"[^\w-]+", "_", unicodedata.normalize("NFC", name or "")).strip("_")[:NAME_PART_MAX] or fallback


def folder_key(name: str) -> str:
    return safe_name(name).casefold()


def has_originals(collection: Path) -> bool:
    return (collection / MANIFEST_NAME).is_file() or any(
        any((collection / ALBUMS).glob(f"{platform}-*/{MANIFEST_NAME}")) for platform in ORIGINAL_PLATFORMS)


def manifest_of(collection: Path, album_key: str | None) -> Path:
    return collection / MANIFEST_NAME if album_key is None else collection / ALBUMS / album_key / MANIFEST_NAME


def album_site(album_key: str | None, registered: dict) -> tuple[str | None, str | None]:
    if album_key is None:
        return None, None
    if album := registered.get(album_key):
        return album.platform, album.site_id
    platform, _, site_id = album_key.partition("-")
    return platform, site_id or None


def read_manifest(path: Path, platform: str | None, ids: list[str]) -> tuple[str | None, dict]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)) as m:
        names = {n for n, in m.execute("select name from sqlite_master")}
        if platform in (None, "yipai") and "photos" in names:
            nums = [int(i) for i in ids if i.isdigit()]
            return "yipai", {str(pid): (fname, order, None) for pid, order, fname in m.execute(
                f"select photo_id, order_id, fname from photos where photo_id in ({','.join('?' * len(nums))})",
                nums)}
        if "catalog" not in names:
            return platform, {}
        return platform, {sid: (fname, None, shot) for sid, fname, shot in m.execute(
            f"select source_id, fname, taken_at from catalog where source_id in ({','.join('?' * len(ids))})", ids)}


def sites_of(collection: Path, metas: list[dict]) -> list[dict]:
    registered = {a.key: a for r in races.load().races for a in r.albums}
    by_album = {}
    for meta in metas:
        by_album.setdefault(meta.get("album_key"), set()).add(meta["source_photo_id"] or "")
    found = {}
    for key, ids in by_album.items():
        platform, site_id = album_site(key, registered)
        path = manifest_of(collection, key)
        rows = {}
        if path.is_file():
            platform, rows = read_manifest(path, platform, sorted(ids - {""}))
        found[key] = platform, site_id, rows
    out = []
    for meta in metas:
        platform, site_id, rows = found[meta.get("album_key")]
        fname, order, shot = rows.get(meta["source_photo_id"] or "", (None, None, None))
        site = sources.site_link(platform, order if platform == "yipai" else site_id, fname) \
            if (meta["source_photo_id"] or "") in rows else None
        out.append({"platform": platform, "fname": fname, "site": site, "order_id": order, "site_id": site_id,
                    "shot_at": shot})
    return out


def profile_folder(collection: Path, profile: str) -> Path:
    return config.DATA_ROOT / "exports" / collection.name / safe_name(profile)


def file_name(meta: dict, platform: str | None = None) -> str:
    t = meta["taken_at"]
    stamp = t.replace("-", "").replace(":", "").replace(" ", "-") if t else "undated"
    sid = safe_name(meta['source_photo_id'], 'none')
    return f"{stamp}_{safe_name(meta['photographer'], 'unknown')}_{platform + '-' if platform in PREFIXED else ''}{sid}.jpg"


def yipai_id(meta: dict) -> int | None:
    spid = meta["source_photo_id"] or ""
    return int(spid) if spid.isdigit() else None


def rows_of(collection: Path, metas: list[dict]) -> list[dict]:
    return [{**meta, "preview": collection / meta["relpath"], "file": file_name(meta, site["platform"]), **site}
            for meta, site in zip(metas, sites_of(collection, metas))]


def downloadable(row: dict) -> bool:
    return row["platform"] in (None, *ORIGINAL_PLATFORMS)


def declared_complete(data: bytes, url: str = "") -> bool:
    declared = re.search(r":(\d+)\.[A-Za-z]+$", urlsplit(url).path)
    return len(data) > 1024 and data[:2] == b"\xff\xd8" and (not declared or len(data) == int(declared[1]))


def sized_complete(size: int | None):
    return lambda data, url: yipai.looks_like_jpeg(data) and (size is None or len(data) == size)


def valid(path: Path, platform: str | None = None) -> bool:
    if not path.is_file():
        return False
    data = path.read_bytes()
    return declared_complete(data) if platform in DECLARED else yipai.looks_like_jpeg(data)


def read_csv(folder: Path) -> dict:
    try:
        with open(folder / CSV_NAME, newline="", encoding="utf-8-sig") as f:
            return {(r["source_photo_id"], r["original_file_name"] or "", r.get("site_url")): r["status"]
                    for r in csv.DictReader(f) if r["status"] != OPEN_ON_SITE}
    except (OSError, KeyError, csv.Error, UnicodeDecodeError):
        return {}


def statuses(folder: Path, rows: list[dict]) -> dict:
    old = read_csv(folder)
    out = {}
    for r in rows:
        if not downloadable(r):
            out[r["photo_id"]] = OPEN_ON_SITE
        elif (folder / ORIGINALS / r["file"]).is_file():
            out[r["photo_id"]] = DOWNLOADED
        else:
            key = r["source_photo_id"] or "", r["fname"] or ""
            s = old.get((*key, r["site"]["url"] if r["site"] else "")) or old.get((*key, None))
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
            original = original if downloadable(r) and original.is_file() else ""
            w.writerow([r["source_photo_id"] or "", r["fname"] or "", r["photographer"] or "", r["taken_at"] or "",
                        r["album"] or "", r["group"] or "", r["preview"], original,
                        status[r["photo_id"]] or "", r["site"]["url"] if r["site"] else ""])
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
        self.locator = photoplus.Locator(self.client, pause=self.pause, clock=clock, tries=tries)
        self.plx_locator = pailixiang.Locator(self.client, pause=self.pause, clock=clock, tries=tries)
        self.xxpie_locator = xxpie.Locator(self.client, pause=self.pause, clock=clock, tries=tries)
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
        return self.download([img["primary"] + img["path"] + img["sign"], img["failover"] + img["path"] + img["sign"]],
                             yipai.HEADERS)

    def download(self, urls: list[str], headers: dict, complete=None, truncated="truncated JPEG") -> bytes:
        complete = complete or (lambda data, url: yipai.looks_like_jpeg(data))
        self.pause(random.uniform(*self.img_delay))
        error = None
        for attempt in range(self.tries):
            wait = yipai.backoff_seconds(attempt)
            try:
                url = urls[attempt % len(urls)]
                r = self.client.get(url, headers=headers)
            except httpx.HTTPError as e:
                error = repr(e)
            else:
                if r.status_code == 403:
                    raise Forbidden("HTTP 403")
                if r.status_code == 200:
                    if complete(r.content, url):
                        return r.content
                    if r.content[:2] != b"\xff\xd8":
                        raise BuyOnSite("not a JPEG")
                    error = truncated
                elif r.status_code in yipai.RETRYABLE:
                    error = f"HTTP {r.status_code}"
                    wait = max(wait, yipai.retry_after(r))
                else:
                    raise Failed(f"HTTP {r.status_code}")
            if attempt < self.tries - 1:
                self.pause(wait)
        raise Failed(error)

    def locate(self, row: dict) -> tuple[str, int | None]:
        platform, site_id, sid = row["platform"], row["site_id"], row["source_photo_id"]
        if platform == "photoplus":
            return self.locator.locate(site_id, row["shot_at"], sid), None
        if platform == "pailixiang":
            return self.plx_locator.locate(site_id, row["fname"], sid)
        return self.xxpie_locator.locate(site_id, row["fname"], sid), None

    def listed_original(self, row: dict) -> bytes:
        platform = row["platform"]
        headers = {"photoplus": photoplus.HEADERS, "pailixiang": pailixiang.IMAGE_HEADERS,
                   "xxpie": xxpie.HEADERS}[platform]
        for relisted in (False, True):
            try:
                url, size = self.locate(row)
            except NotFound as e:
                raise Failed(str(e))
            if not url:
                raise BuyOnSite("no original URL")
            try:
                return self.download([url], headers, sized_complete(size) if platform == "pailixiang" else
                                     declared_complete, "truncated download")
            except Forbidden:
                if relisted:
                    raise Failed(f"{platform} refused the download link again after relisting (HTTP 403)")
                if platform == "photoplus":
                    self.locator.forget(row["site_id"], row["source_photo_id"])

    def yipai_original(self, row: dict) -> bytes:
        return self.fetch(self.lookup(row["order_id"], row["fname"], yipai_id(row)))

    def original(self, row: dict, dest: Path) -> str:
        if not downloadable(row):
            return OPEN_ON_SITE
        dest.unlink(missing_ok=True)
        listed = row["platform"] in PREFIXED
        if row["platform"] is None or (listed and row["site"] is None) or (
                row["platform"] != "photoplus" and not row["fname"]):
            return "failed: not in the gallery manifest"
        try:
            data = self.listed_original(row) if listed else self.yipai_original(row)
        except yipai.Blocked as e:
            raise Unavailable(f"{PLATFORM_NAMES[row['platform']]} API unavailable: {e}", row["platform"]) from e
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

    def attempt(self, row: dict, dest: Path, rest: list[dict], down: dict) -> str:
        platform = row["platform"]
        if platform in down:
            return f"failed: {down[platform]}"
        try:
            return self.fetcher.original(row, dest)
        except Unavailable as e:
            down[platform] = str(e)
            if all(r["platform"] in down for r in rest):
                raise
            return f"failed: {e}"

    def run(self, folder: Path, rows: list[dict]):
        state, error = "done", None
        down = {}
        try:
            for i, row in enumerate(rows):
                self.fetcher.pause(0)
                self.update(current=row["fname"] or row["relpath"])
                dest = folder / ORIGINALS / row["file"]
                if downloadable(row) and valid(dest, row['platform']):
                    key, result = "skipped", DOWNLOADED
                else:
                    result = self.attempt(row, dest, rows[i + 1:], down)
                    key = "downloaded" if result == DOWNLOADED else result.split(":")[0].replace(" ", "_")
                    write_csv(folder, rows, {row["photo_id"]: result})
                with self.lock:
                    self.state["counts"][key] += 1
                    self.state["done"] = i + 1
                    if result not in (DOWNLOADED, OPEN_ON_SITE):
                        self.state["errors"].append(f"{row['source_photo_id']} {row['fname']}: {result}")
        except Cancelled:
            state = "cancelled"
        except Unavailable:
            state, error = "error", "; ".join(down.values())
            if len(down) > 1:
                write_csv(folder, rows, {r["photo_id"]: f"failed: {down[r['platform']]}"
                                         for r in rows[i:] if r["platform"] in down})
        except Exception as e:
            log.exception("originals job failed")
            state, error = "error", f"{type(e).__name__}: {e}"
        else:
            if down:
                state, error = "error", "; ".join(down.values())
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
        result = DOWNLOADED if downloadable(row) and valid(dest, row['platform']) else self.fetcher.original(row, dest)
        if any(r["photo_id"] == row["photo_id"] for r in rows):
            write_csv(folder, rows, {row["photo_id"]: result})
        return result, dest
