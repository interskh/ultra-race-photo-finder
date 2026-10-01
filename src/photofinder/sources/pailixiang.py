import logging
import random
import re
import time

import httpx

from photofinder.sources.base import CatalogRow
from photofinder.sources.common import NotFound, Paced, fetch_json

API = "https://mapi.pailixiang.com/plx"
SITE = "https://live.pailixiang.com"
KEY = "REMOVED-pailixiang-web-client-key"  # public web-client app key from the site's index.js; gitleaks:allow
HEADERS = {
    "Referer": f"{SITE}/",
    "Origin": SITE,
    "Content-Type": "application/json;charset=UTF-8",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
}
IMAGE_HEADERS = {k: v for k, v in HEADERS.items() if k != "Content-Type"}
LOOKUP_GAP = 2.5
MAX_LOOKUP_PAGES = 5
COMMON = {"tt": "", "ct": 0, "cv": "169", "lang": "cn", "pid": "albumview"}
PAGE = 80
SHOT_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")

log = logging.getLogger("download")


def ak():
    t = list(KEY)
    n = ""
    for _ in range(3):
        e = random.randrange(10)
        n += str(e)
        t[e + 15] = t[e]
    return n + "".join(t)


def ok(body):
    if body.get("Code") != 0:
        raise ValueError(f"pailixiang Code {body.get('Code')} {body.get('Msg') or ''}".rstrip())
    return body


def to_row(p: dict) -> CatalogRow:
    shot = p.get("ShootTime")
    return CatalogRow(str(p["ID"]), fname=p.get("Name"), photographer_uid=p.get("CreateUserID"),
                      photographer=p.get("CreateUserName"), group_name=None,
                      taken_at=shot if isinstance(shot, str) and SHOT_TIME.fullmatch(shot) else None,
                      width=p.get("Width"), height=p.get("Height"), url=p.get("BigImageUrl"))


def site_link(site_id: str, fname: str | None) -> dict:
    return {"url": f"{SITE}/album/{site_id}", "exact": False, "find_by": fname,
            "hint": "look near the shot time (照片直播 order is loose); 照片信息 under a photo shows file name and shot time"}


class Adapter:
    def __init__(self, client: httpx.Client, site_id: str, *, tries=5, sleep=time.sleep, headers=None):
        self.client = client
        self.headers = headers or {}
        self.code = site_id.removeprefix("a")
        self.tries = tries
        self.sleep = sleep
        self.album_id = None
        self.opt_time = None
        self.total = None
        self.skipped = set()

    def post(self, action: str, body: dict) -> dict:
        return fetch_json(self.client, "POST", f"{API}/WapAbm/{action}", check=ok, tries=self.tries,
                          sleep=self.sleep, fresh=lambda: {"json": {**body, **COMMON, "ak": ak()}, "headers": self.headers})

    def meta(self) -> dict:
        entity = self.post("AlbumGetView", {"ID": self.code, "AccessType": "1", "ClientType": 0})["Data"]["Entity"]
        self.album_id = entity["ID"]
        return {"title": entity.get("Title") or None, "total": None}

    def meta_items(self) -> dict:
        return {"album_id": self.album_id}

    def search(self, start: int, **extra) -> dict:
        return self.post("AlbumSearchPhoto", {
            "AlbumID": self.album_id, "GroupID": "", "SearchType": 0, "IsPayDownload": False, "PhotoSortType": 1,
            "IsNw": False, "IsEmbed": False, "StartIndex": start, "SearchCount": PAGE, "SortType": 1,
            "OptTime": self.opt_time or "", **extra})

    def list_page(self, cursor):
        start = cursor or 1
        body = self.search(start)
        if self.opt_time is None:
            self.opt_time = body.get("OptTime") or ""
        data = body.get("Data") or []
        rows = []
        for p in data:
            sid = p.get("ID")
            if sid is not None and SAFE_ID.fullmatch(str(sid)):
                rows.append(to_row(p))
            else:
                self.skipped.add((str(sid), p.get("FileName"), p.get("Name")))
                log.warning("skipping pailixiang photo with unusable ID %r (%s)", sid, p.get("Name"))
        count = body.get("TotalCount")
        if self.total is None and isinstance(count, int) and count > 0:
            self.total = count
        total = None if self.total is None else self.total - len(self.skipped)
        return rows, (start + PAGE if len(data) >= PAGE else None), total

    def preview_url(self, row: CatalogRow) -> str:
        return row.url


class Locator(Paced):
    def __init__(self, client: httpx.Client, *, pause, clock=time.monotonic, tries=5, gap=LOOKUP_GAP):
        super().__init__(pause, clock, gap)
        self.client, self.tries = client, tries
        self.adapters = {}

    def adapter(self, site_id: str) -> Adapter:
        if site_id not in self.adapters:
            a = Adapter(self.client, site_id, tries=self.tries, sleep=self.retry_sleep, headers=HEADERS)
            self.call(a.meta)
            self.adapters[site_id] = a
        return self.adapters[site_id]

    def locate(self, site_id: str, fname: str, source_id: str) -> tuple[str, int | None]:
        a = self.adapter(site_id)
        for n in range(MAX_LOOKUP_PAGES):
            data = self.call(lambda: a.search(1 + n * PAGE, SearchText=fname)).get("Data") or []
            for p in data:
                if str(p.get("ID")) == source_id:
                    size = p.get("FileSize1")
                    return p.get("DownloadImageUrl") or "", size if type(size) is int else None
            if len(data) < PAGE:
                raise NotFound("not found in the pailixiang album")
        raise NotFound(f"lookup limit reached after {MAX_LOOKUP_PAGES} pages of results for {fname}")
