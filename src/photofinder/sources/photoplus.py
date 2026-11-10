import hashlib
import json
import logging
import math
import re
import time

import httpx

from photofinder.sources.base import CatalogRow
from photofinder.sources.common import NotFound, fetch_json, site_key

API = "https://live.photoplus.cn"
HEADERS = {
    "Referer": f"{API}/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
}
LIST_PAGE = 100
ALBUM_PAGE = 200
PAGE_GAP = 2.5
PAGE_TTL = 240
MAX_PAGE_REQUESTS = 24
SHOT_TIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")

SEALED = "listing:albums"
UNSET = object()

log = logging.getLogger("download")


def count_of(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def anchor(pages, pos, p, step, last, n):
    while p in pages:
        for i in (pages[p] if step > 0 else reversed(pages[p])):
            if i in pos:
                return pos[i]
        p += step
    return -1 if p < 1 else n if p > last else None


def bisect_pages(old, m, size, npages):
    n, pos, pages, gaps = len(old), {i: k for k, i in enumerate(old)}, {}, {}
    for p in sorted({1, npages}):
        pages[p] = yield p
    while True:
        got = sorted(pages)
        open_region = None
        for a, b in zip(got, got[1:]):
            if b == a + 1 or (a, b) in gaps:
                continue
            left, right = anchor(pages, pos, a, -1, npages, n), anchor(pages, pos, b, 1, npages, n)
            if left is not None and right is not None and right - left - 1 == (b - a - 1) * size:
                gaps[(a, b)] = old[left + 1:right]
            elif open_region is None:
                open_region = (a, b)
        if open_region is None:
            break
        mid = sum(open_region) // 2
        pages[mid] = yield mid
    new = []
    for a, b in zip(got, got[1:]):
        new += pages[a] + gaps.get((a, b), [])
    new += pages[got[-1]]
    return new if len(new) == m and len(set(new)) == len(new) else None


def bisect_listing(old, m, size):
    npages = math.ceil(m / size) if m else 0
    why = "no snapshot"
    if old and npages and len(set(old)) == len(old):
        new = yield from bisect_pages(old, m, size, npages)
        if new is not None:
            return new, "bisect"
        why = "inconsistent listing"
    new, page = [], 1
    while page <= npages + 1:
        ids = yield page
        new += ids
        if len(ids) < size:
            break
        page += 1
    return new, f"linear read ({why})"


def sign(params: dict, t: int) -> dict:
    p = {k: v for k, v in {**params, "_t": t}.items() if v is not None}
    text = "&".join(f"{k}={json.dumps(p[k], separators=(',', ':'), ensure_ascii=False)}".replace('"', '')
                    for k in sorted(p))
    query = {k: json.dumps(v) if isinstance(v, bool) else v for k, v in p.items()}
    return {**query, "_s": hashlib.md5((text + site_key("photoplus_salt")).encode()).hexdigest()}


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
        self.db = None
        self.full = True
        self.mode = None
        self.staged = {}
        self.list_count = None
        self.reset_diff()

    def reset_diff(self):
        self.keys, self.cur, self.gen, self.want = [], None, None, None
        self.counts, self.by_key, self.sealed = {}, {}, []
        self.preload, self.entry, self.last, self.step = None, UNSET, None, 0
        self.requests = {}

    def attach(self, db, full: bool):
        self.db, self.full, self.mode, self.staged = db, full, None, {}
        self.reset_diff()
        db.execute("create table if not exists listing(list_key text not null, pos integer not null, "
                   "source_id text not null, primary key(list_key, pos))")
        db.execute("create table if not exists gone(source_id text primary key, noticed_at text not null)")
        db.commit()

    def stored(self, key: str):
        row = self.db.execute("select value from meta where key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def plan(self) -> str:
        raw = self.stored(SEALED)
        if raw is None:
            log.info("photoplus: no complete listing snapshot yet; listing everything")
            return "full"
        if self.db.execute("select 1 from catalog where status != 'done' limit 1").fetchone():
            log.info("photoplus: some photos are not downloaded yet; listing everything for fresh URLs")
            return "full"
        result = self.get("/pic/list", {"activityNo": self.activity, "key": "", "isNew": False, "count": LIST_PAGE,
                                         "page": 1, "size": 2000, "ppSign": ""}) or {}
        now = {f"album:{a['album_id']}": count_of(a.get("pic_num")) for a in self.albums}
        now["list"] = count_of(result.get("pics_total"))
        old = {k: self.stored(f"count:{k}") for k in [*json.loads(raw), "list"]}
        changed = [(k, old.get(k), now.get(k)) for k in [*old, *(k for k in now if k not in old)]
                   if now.get(k) is None or old.get(k) != str(now[k])]
        for k, before, after in changed:
            log.info("photoplus: %s changed %s -> %s", k, before, after)
        if not changed:
            log.info("photoplus: unchanged since last check: %d listings, %s photos", len(now), now["list"])
            return "unchanged"
        if any(c is None for c in now.values()):
            log.info("photoplus: a listing has no usable count; listing everything")
            return "full"
        self.sealed = json.loads(raw)
        self.counts, self.list_count = now, now["list"]
        self.by_key = {f"album:{a['album_id']}": a for a in self.albums}
        self.preload = result.get("pics_array") or []
        names = {k for k, _, _ in changed}
        self.keys = [k for k in now if k in names]
        self.start_next()
        return "diff"

    def start_next(self):
        self.cur = None
        if not self.keys:
            return
        key = self.keys.pop(0)
        stored = self.stored(f"count:{key}") is not None and key in [*self.sealed, "list"]
        old = [r[0] for r in self.db.execute("select source_id from listing where list_key=? order by pos",
                                              (key,))] if stored else None
        self.cur = key
        self.requests[key] = 0
        self.gen = bisect_listing(old, self.counts[key], LIST_PAGE if key == "list" else ALBUM_PAGE)
        self.want = next(self.gen)

    def fetch(self, key: str, page: int) -> list:
        self.requests[key] += 1
        if key == "list":
            result = self.get("/pic/list", {"activityNo": self.activity, "key": "", "isNew": False,
                                             "count": LIST_PAGE, "page": page, "size": 2000, "ppSign": ""})
            return (result or {}).get("pics_array") or []
        result = self.get("/album/one", {"albumId": self.by_key[key]["album_id"], "count": ALBUM_PAGE,
                                         "size": ALBUM_PAGE, "page": page, "ppSign": "", "picUpIndex": ""})
        return (result or {}).get("pics") or []

    def take(self, key: str, pics: list) -> list[CatalogRow]:
        name = (self.by_key[key].get("name") or None) if key != "list" else None
        out = []
        for p in pics:
            sid = p.get("id")
            if sid is None or not SAFE_ID.fullmatch(str(sid)):
                self.skipped.add((str(sid), p.get("pic_name")))
                log.warning("skipping photoplus photo with unusable id %r (%s)", sid, p.get("pic_name"))
                continue
            sid = str(sid)
            group = self.group_of.setdefault(sid, name) if key != "list" else self.group_of.get(sid)
            known = self.db.execute("select group_name from catalog where source_id=?", (sid,)).fetchone()
            if known is None:
                row = to_row(p, group)
                self.db.execute("insert or ignore into catalog(source_id, fname, photographer_uid, photographer, "
                                "group_name, taken_at, width, height) values (?,?,?,?,?,?,?,?)",
                                (row.source_id, row.fname, row.photographer_uid, row.photographer, row.group_name,
                                 row.taken_at, row.width, row.height))
                out.append(row)
            elif known[0] is None and name:
                self.db.execute("update catalog set group_name=? where source_id=? and group_name is null",
                                (name, sid))
        self.db.commit()
        return out

    def advance(self, ids: list):
        try:
            self.want = self.gen.send(ids)
        except StopIteration as done:
            new, how = done.value
            removed = self.store(self.cur, new, self.counts[self.cur])
            log.info("photoplus: %s: %d photos by %s, %d requests", self.cur, len(new), how,
                     self.requests[self.cur])
            if removed:
                log.info("photoplus: %d photos were removed from the album since the last check; kept locally",
                         removed)
            self.start_next()

    def diff_page(self, cursor):
        if self.last is not None and cursor == self.entry:
            rows = []
            for key, page in self.last["pages"]:
                rows += [to_row(p, self.last["groups"][str(p.get("id"))]) for p in self.fetch(key, page)
                         if str(p.get("id")) in self.last["groups"]]
            return rows, self.last["nxt"], None
        self.entry = cursor
        rows, pages, requested = [], [], False
        while self.cur is not None and not requested:
            key, page = self.cur, self.want
            if key == "list" and page == 1 and self.preload is not None:
                pics, self.preload = self.preload, None
            else:
                pics, requested = self.fetch(key, page), True
            pages.append((key, page))
            rows += self.take(key, pics)
            self.advance([str(p.get("id")) for p in pics])
        if self.cur is None:
            self.seal()
            nxt = None
        else:
            self.step += 1
            nxt = ("diff", self.step)
        self.last = {"pages": pages, "groups": {r.source_id: r.group_name for r in rows}, "nxt": nxt}
        return rows, nxt, None

    def stage(self, key: str, page: int, pics: list):
        if self.db is not None:
            self.staged.setdefault(key, {})[page] = [str(p.get("id")) for p in pics]

    def finish(self, key: str, count):
        if self.db is None:
            return
        pages = self.staged.pop(key, {})
        self.store(key, [i for n in sorted(pages) for i in pages[n]], count)

    def store(self, key: str, ids: list, count) -> int:
        removed = 0
        if key == "list":
            before = [r[0] for r in self.db.execute("select source_id from listing where list_key='list'")]
            now = set(ids)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
            lost = [(i, stamp) for i in before if i not in now]
            removed = len(lost)
            self.db.executemany("insert or ignore into gone values (?,?)", lost)
            back = now.intersection(r[0] for r in self.db.execute("select source_id from gone"))
            self.db.executemany("delete from gone where source_id=?", [(i,) for i in back])
        self.db.execute("delete from listing where list_key=?", (key,))
        self.db.executemany("insert into listing values (?,?,?)", [(key, n, i) for n, i in enumerate(ids)])
        if count is None:
            self.db.execute("delete from meta where key=?", (f"count:{key}",))
        else:
            self.db.execute("insert or replace into meta values (?,?)", (f"count:{key}", str(count)))
        self.db.commit()
        return removed

    def seal(self):
        if self.db is None:
            return
        keys = [f"album:{a['album_id']}" for a in self.albums]
        counts = [count_of(a.get("pic_num")) for a in self.albums] + [self.list_count]
        if any(c is None for c in counts):
            return
        marks = ",".join("?" * (len(keys) + 1))
        self.db.execute(f"delete from listing where list_key not in ({marks})", [*keys, "list"])
        self.db.execute(f"delete from meta where key like 'count:%' and substr(key, 7) not in ({marks})",
                        [*keys, "list"])
        self.db.execute("insert or replace into meta values (?,?)", (SEALED, json.dumps(keys)))
        self.db.commit()

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
        if self.mode is None:
            self.mode = self.plan() if self.db is not None and not self.full else "full"
            if self.mode == "full" and self.db is not None:
                self.db.execute("delete from meta where key=?", (SEALED,))
                self.db.commit()
        if self.mode == "unchanged":
            return [], None, None
        if self.mode == "diff":
            return self.diff_page(cursor)
        cursor = cursor or (("album", 0, 1) if self.albums else ("list", 1))
        if cursor[0] == "album":
            _, i, page = cursor
            album = self.albums[i]
            pics = (self.get("/album/one", {"albumId": album["album_id"], "count": ALBUM_PAGE, "size": ALBUM_PAGE,
                                            "page": page, "ppSign": "", "picUpIndex": ""}) or {}).get("pics") or []
            rows = self.rows(pics, album.get("name") or None)
            self.stage(f"album:{album['album_id']}", page, pics)
            if len(pics) < ALBUM_PAGE:
                self.finish(f"album:{album['album_id']}", count_of(album.get("pic_num")))
            nxt = (("album", i, page + 1) if len(pics) >= ALBUM_PAGE else
                   ("album", i + 1, 1) if i + 1 < len(self.albums) else ("list", 1))
        else:
            page = cursor[1]
            result = self.get("/pic/list", {"activityNo": self.activity, "key": "", "isNew": False,
                                             "count": LIST_PAGE, "page": page, "size": 2000, "ppSign": ""}) or {}
            count = result.get("pics_total")
            if page == 1:
                self.list_count = count_of(count)
            if self.total is None and isinstance(count, int) and not isinstance(count, bool) and count > 0:
                self.total = count
            pics = result.get("pics_array") or []
            rows = self.rows(pics, None)
            self.stage("list", page, pics)
            seen = len(self.group_of) + len(self.skipped)
            if self.total is None:
                more = len(pics) >= LIST_PAGE
            elif seen > self.total:
                if not self.warned:
                    self.warned = True
                    log.warning("photoplus: seen %d distinct photos vs pics_total %d; paging /pic/list to a short page",
                                seen, self.total)
                more = len(pics) >= LIST_PAGE
            else:
                more = page < math.ceil(self.total / LIST_PAGE)
            nxt = ("list", page + 1) if more else None
            if nxt is None:
                self.finish("list", self.list_count)
                self.seal()
        return rows, nxt, None if self.total is None else self.total - len(self.skipped)

    def preview_url(self, row: CatalogRow) -> str:
        return row.url


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
