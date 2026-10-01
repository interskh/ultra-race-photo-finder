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
PAGE_GAP = 2.5
PAGE_TTL = 240
MAX_PAGE_REQUESTS = 24
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
            "hint": "open the group tab; the ⓘ icon under a photo shows its file name"}


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


class NotFound(Exception):
    pass


class BudgetExceeded(NotFound):
    pass


def https(url):
    return "https:" + url if isinstance(url, str) and url.startswith("//") else url


class Locator:
    def __init__(self, client: httpx.Client, *, pause, clock=time.monotonic, tries=5, gap=PAGE_GAP, ttl=PAGE_TTL,
                 budget=MAX_PAGE_REQUESTS):
        self.client, self.pause, self.clock = client, pause, clock
        self.tries, self.gap, self.ttl, self.budget = tries, gap, ttl, budget
        self.pages = {}
        self.found = {}
        self.last = None

    def page(self, activity: str, n: int, spent: list) -> tuple[list, int]:
        hit = self.pages.get((activity, n))
        if hit and self.clock() - hit[0] < self.ttl:
            spent[1] += 1
            return hit[1], hit[2]
        if spent[0] >= self.budget:
            raise BudgetExceeded("photoplus listing request limit reached while locating the photo; try again later")
        if self.last is not None:
            self.pause(self.last + self.gap - self.clock())
        params = {"activityNo": activity, "key": "", "isNew": False, "count": LIST_PAGE, "page": n, "size": 2000,
                  "ppSign": ""}

        def attempt():
            if spent[0] >= self.budget:
                raise BudgetExceeded("photoplus listing request limit reached while locating the photo; "
                                     "try again later")
            spent[0] += 1
            return {"params": sign(params, int(time.time() * 1000)), "headers": HEADERS}
        try:
            result = fetch_json(self.client, "GET", f"{API}/pic/list", check=ok, tries=self.tries,
                                sleep=lambda wait: self.pause(max(wait, self.gap)), fresh=attempt)
        finally:
            self.last = self.clock()
        pics = (result or {}).get("pics_array") or []
        total = (result or {}).get("pics_total")
        if not isinstance(total, int) or isinstance(total, bool):
            raise NotFound("photoplus did not report the album size")
        self.pages[(activity, n)] = self.clock(), pics, total
        return pics, total

    def locate(self, activity: str, shot: str | None, source_id: str) -> str:
        if not shot:
            raise NotFound("no shot time recorded")
        spent = [0, 0]
        try:
            return self.search(activity, shot, source_id, spent)
        except BudgetExceeded:
            raise
        except NotFound:
            if not spent[1]:
                raise
        for key in [k for k in self.pages if k[0] == activity]:
            del self.pages[key]
        return self.search(activity, shot, source_id, [0, 0])

    def search(self, activity, shot, source_id, spent) -> str:
        _, total = self.page(activity, 1, spent)
        pages = max(1, math.ceil(total / LIST_PAGE))
        lo, hi = 1, pages
        while lo <= hi:
            mid = (lo + hi) // 2
            pics, total = self.page(activity, mid, spent)
            pages = max(1, math.ceil(total / LIST_PAGE))
            if not pics or shot > (pics[0].get("relate_time") or ""):
                hi = mid - 1
            elif shot < (pics[-1].get("relate_time") or ""):
                lo = mid + 1
            else:
                return self.scan(activity, mid, pages, shot, source_id, spent)
        raise NotFound("not found in the photoplus album")

    def scan(self, activity, mid, pages, shot, source_id, spent) -> str:
        def find(pics, n):
            for p in pics:
                if str(p.get("id")) == source_id:
                    self.found[(activity, source_id)] = n
                    return https(p.get("watermark_origin_img")) or ""
        pics, _ = self.page(activity, mid, spent)
        if (url := find(pics, mid)) is not None:
            return url
        for step, edge in ((-1, pics[0]), (1, pics[-1])):
            n = mid + step
            while edge.get("relate_time") == shot and 1 <= n <= pages:
                near, _ = self.page(activity, n, spent)
                if not near:
                    break
                first, last = near[0], near[-1]
                if (first if step > 0 else last).get("relate_time") != shot:
                    break
                if (url := find(near, n)) is not None:
                    return url
                edge = last if step > 0 else first
                n += step
        raise NotFound("not found in the photoplus album")

    def forget(self, activity: str, source_id: str):
        self.pages.pop((activity, self.found.pop((activity, source_id), None)), None)
