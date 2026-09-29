import hashlib
import json
import logging
import math
import re
import time

import httpx

from photofinder.sources.base import CatalogRow
from photofinder.sources.common import fetch_json

API = "https://live.photoplus.cn"
HEADERS = {
    "Referer": f"{API}/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
}
SALT = "REMOVED-photoplus-salt"
LIST_PAGE = 100
ALBUM_PAGE = 200
SHOT_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")

log = logging.getLogger("download")


def sign(params: dict, t: int) -> dict:
    p = {k: v for k, v in {**params, "_t": t}.items() if v is not None}
    text = "&".join(f"{k}={json.dumps(p[k], separators=(',', ':'), ensure_ascii=False)}".replace('"', '')
                    for k in sorted(p))
    query = {k: json.dumps(v) if isinstance(v, bool) else v for k, v in p.items()}
    return {**query, "_s": hashlib.md5((text + SALT).encode()).hexdigest()}


def ok(body):
    if body.get("code") != 1:
        raise ValueError(f"photoplus code {body.get('code')} {body.get('message') or ''}".rstrip())
    return body.get("result")


def to_row(p: dict, group: str | None) -> CatalogRow:
    uid, who = (p.get("camer_no"), p.get("camer")) if p.get("camer") else (p.get("retoucher_no"), p.get("retoucher"))
    shot = p.get("relate_time")
    url = p.get("big_img")
    if isinstance(url, str) and url.startswith("//"):
        url = "https:" + url
    return CatalogRow(str(p["id"]), fname=p.get("pic_name"), photographer_uid=None if uid is None else str(uid),
                      photographer=who or None, group_name=group,
                      taken_at=shot if isinstance(shot, str) and SHOT_TIME.fullmatch(shot) else None,
                      width=p.get("width"), height=p.get("height"), url=url)


def site_link(site_id: str, fname: str | None) -> dict:
    return {"url": f"{API}/live/{site_id}?accessFrom=live#/live", "exact": False, "find_by": fname,
            "hint": "open the group, sort by time; file name in the photo info"}


class Adapter:
    def __init__(self, client: httpx.Client, site_id: str, *, tries=5, sleep=time.sleep, clock=time.time):
        self.client = client
        self.activity = site_id
        self.tries = tries
        self.sleep = sleep
        self.clock = clock
        self.albums = None
        self.group_of = {}
        self.total = None
        self.skipped = set()
        self.warned = False

    def get(self, path: str, params: dict):
        return fetch_json(self.client, "GET", f"{API}{path}", check=ok, tries=self.tries, sleep=self.sleep,
                          fresh=lambda: {"params": sign(params, int(self.clock() * 1000))})

    def meta(self) -> dict:
        return {"title": (self.get("/live/detail", {"activityNo": self.activity}) or {}).get("name") or None,
                "total": None}

    def rows(self, pics: list, group: str | None) -> list[CatalogRow]:
        out = []
        for p in pics:
            sid = p.get("id")
            if sid is None or not SAFE_ID.fullmatch(str(sid)):
                self.skipped.add((str(sid), p.get("pic_name")))
                log.warning("skipping photoplus photo with unusable id %r (%s)", sid, p.get("pic_name"))
            else:
                out.append(to_row(p, self.group_of.setdefault(str(sid), group)))
        return out

    def list_page(self, cursor):
        if self.albums is None:
            self.albums = self.get("/album/albums", {"activityNo": self.activity, "count": 1000}) or []
        cursor = cursor or (("album", 0, 1) if self.albums else ("list", 1))
        if cursor[0] == "album":
            _, i, page = cursor
            album = self.albums[i]
            pics = (self.get("/album/one", {"albumId": album["album_id"], "count": ALBUM_PAGE, "size": ALBUM_PAGE,
                                            "page": page, "ppSign": "", "picUpIndex": ""}) or {}).get("pics") or []
            rows = self.rows(pics, album.get("name") or None)
            nxt = (("album", i, page + 1) if len(pics) >= ALBUM_PAGE else
                   ("album", i + 1, 1) if i + 1 < len(self.albums) else ("list", 1))
        else:
            page = cursor[1]
            result = self.get("/pic/list", {"activityNo": self.activity, "key": "", "isNew": False,
                                             "count": LIST_PAGE, "page": page, "size": 2000, "ppSign": ""}) or {}
            count = result.get("pics_total")
            if self.total is None and isinstance(count, int) and not isinstance(count, bool) and count > 0:
                self.total = count
            pics = result.get("pics_array") or []
            rows = self.rows(pics, None)
            seen = len(self.group_of) + len(self.skipped)
            if self.total is None:
                more = len(pics) >= LIST_PAGE
            elif seen == self.total:
                more = False
            elif seen > self.total:
                if not self.warned:
                    self.warned = True
                    log.warning("photoplus: seen %d distinct photos vs pics_total %d; paging /pic/list to a short page",
                                seen, self.total)
                more = len(pics) >= LIST_PAGE
            else:
                more = page < math.ceil(self.total / LIST_PAGE)
            nxt = ("list", page + 1) if more else None
        return rows, nxt, None if self.total is None else self.total - len(self.skipped)

    def preview_url(self, row: CatalogRow) -> str:
        return row.url
