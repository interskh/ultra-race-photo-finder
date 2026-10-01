import logging
import re
import time
import uuid
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from photofinder.sources.base import CatalogRow
from photofinder.sources.common import NotFound, Paced, fetch_json

API = "https://int.xxpie.com"
SITE = "https://www.xxpie.com"
HEADERS = {
    "Referer": f"{SITE}/",
    "Origin": SITE,
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
}
PAGE = 60
LOOKUP_GAP = 2.5
MAX_LOOKUP_PAGES = 5
RECORD_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")
LOCAL = ZoneInfo("Asia/Shanghai")

log = logging.getLogger("download")


def shot_time(value) -> str | None:
    if not isinstance(value, str) or not RECORD_TIME.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value).astimezone(LOCAL).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def to_row(p: dict) -> CatalogRow:
    who = p.get("photographer") or {}
    return CatalogRow(str(p["album_ossobject_id"]), fname=p.get("file_name"), photographer_uid=who.get("team_id"),
                      photographer=who.get("nick_name"), group_name=None, taken_at=shot_time(p.get("record_time")),
                      width=p.get("width"), height=p.get("height"), url=p.get("url_large1920"))


def registered(body):
    token = (body.get("result") or {}).get("token") if body.get("code") == 0 else None
    if not token:
        raise ValueError(f"xxpie visitor registration code {body.get('code')} {body.get('message') or ''}".rstrip())
    return token


def site_link(site_id: str, fname: str | None) -> dict:
    return {"url": f"{SITE}/m/albumFilenameSearch?album_id={site_id}&search_word={quote(fname or '', safe='')}",
            "exact": bool(fname), "find_by": fname, "hint": None}


class Adapter:
    def __init__(self, client: httpx.Client, site_id: str, *, tries=5, sleep=time.sleep, headers=None):
        self.client = client
        self.headers = headers or {}
        self.album_id = site_id
        self.tries = tries
        self.sleep = sleep
        self.token = None
        self.registrar = None
        self.total = None
        self.skipped = set()

    def register(self):
        self.token = fetch_json(self.client, "POST", f"{API}/api/sm/registerVisitorUser", check=registered,
                                tries=self.tries, sleep=self.sleep,
                                fresh=lambda: {"json": {"username": uuid.uuid4().hex, "platform": "H5"},
                                       "headers": self.headers})

    def auth(self) -> dict:
        if self.token is None:
            (self.registrar or self.register)()
        return {"headers": {**self.headers, "x-access-token": self.token}}

    def ok(self, body):
        if body.get("code") != 0:
            self.token = None
            raise ValueError(f"xxpie code {body.get('code')} {body.get('message') or ''}".rstrip())
        return body.get("result") or {}

    def get(self, path: str, params: dict) -> dict:
        return fetch_json(self.client, "GET", f"{API}/api/pm/{path}", params={**params, "platform": "H5"},
                          check=self.ok, tries=self.tries, sleep=self.sleep, fresh=self.auth)

    def meta(self) -> dict:
        info = self.get("queryAlbumStyleH5", {"album_id": self.album_id, "is_visited": 0, "source": "H5"})["info"]
        count = self.get("querySubAlbumPhotoInfo", {"album_id": self.album_id}).get("photo_count")
        self.total = count if isinstance(count, int) and not isinstance(count, bool) else None
        return {"title": info.get("album_name") or None, "total": self.total}

    def list_page(self, cursor):
        page = cursor or 1
        photos = self.get("queryAlbumItemsPgByDefaultSort", {
            "album_id": self.album_id, "page_no": page, "page_size": PAGE, "sub_album_id": "ALL",
            "no_watermark": ""}).get("photos") or []
        rows = []
        for p in photos:
            sid = p.get("album_ossobject_id")
            if sid is not None and SAFE_ID.fullmatch(str(sid)):
                rows.append(to_row(p))
            else:
                self.skipped.add((str(sid), p.get("file_name")))
                log.warning("skipping xxpie photo with unusable id %r (%s)", sid, p.get("file_name"))
        total = None if self.total is None else self.total - len(self.skipped)
        return rows, (page + 1 if len(photos) >= PAGE else None), total

    def preview_url(self, row: CatalogRow) -> str:
        return row.url


class Locator(Paced):
    def __init__(self, client: httpx.Client, *, pause, clock=time.monotonic, tries=5, gap=LOOKUP_GAP):
        super().__init__(pause, clock, gap)
        self.client, self.tries = client, tries
        self.adapters = {}

    def locate(self, site_id: str, fname: str, source_id: str) -> str:
        if site_id not in self.adapters:
            self.adapters[site_id] = Adapter(self.client, site_id, tries=self.tries, sleep=self.retry_sleep,
                                             headers=HEADERS)
        a = self.adapters[site_id]
        registered = 0

        def renew():
            nonlocal registered
            if registered >= 2:
                raise ValueError("xxpie keeps rejecting the visitor token")
            registered += 1
            if self.last is not None:
                self.pause(self.last + self.gap - self.clock())
            try:
                a.register()
            finally:
                self.last = self.clock()
            self.pause(self.gap)
        a.registrar = renew
        for n in range(1, MAX_LOOKUP_PAGES + 1):
            photos = self.call(lambda: a.get("queryAlbumItemsPgByDefaultSort", {
                "album_id": site_id, "page_no": n, "page_size": PAGE, "file_name": fname})).get("photos") or []
            for p in photos:
                if str(p.get("album_ossobject_id")) == source_id:
                    return p.get("url_origin") or ""
            if len(photos) < PAGE:
                break
        raise NotFound("not found in the xxpie album")
