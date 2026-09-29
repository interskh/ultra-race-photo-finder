import logging
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from photofinder.sources.base import CatalogRow
from photofinder.sources.common import fetch_json

API = "https://int.xxpie.com"
SITE = "https://www.xxpie.com"
HEADERS = {
    "Referer": f"{SITE}/",
    "Origin": SITE,
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
}
PAGE = 60
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


class Adapter:
    def __init__(self, client: httpx.Client, site_id: str, *, tries=5, sleep=time.sleep):
        self.client = client
        self.album_id = site_id
        self.tries = tries
        self.sleep = sleep
        self.token = None
        self.total = None
        self.skipped = set()

    def register(self):
        self.token = fetch_json(self.client, "POST", f"{API}/api/sm/registerVisitorUser", check=registered,
                                tries=self.tries, sleep=self.sleep,
                                fresh=lambda: {"json": {"username": uuid.uuid4().hex, "platform": "H5"}})

    def auth(self) -> dict:
        if self.token is None:
            self.register()
        return {"headers": {"x-access-token": self.token}}

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
