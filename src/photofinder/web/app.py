import asyncio
import io
import logging
import multiprocessing
import os
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Callable, Literal

import numpy as np
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from photofinder import db, models, nearby, originals, races, search
from photofinder.memory import watch_parent
from photofinder.sources.yipai import CATALOG_SELECT
from photofinder.index.stages import ALBUMS, MANIFEST_NAME, now

STATIC = Path(__file__).parent / "static"
UPLOADS = 8
MAX_TOP = 500
CROP_HEIGHT = 256
CROP_PAD = 0.1
CACHE = {"Cache-Control": "private, max-age=86400"}
NAME_MAX = 40
FIRST = "order by ph.taken_at is null, ph.taken_at, ph.id"
Id = Annotated[int, Field(ge=0, le=2 ** 63 - 1)]
IDLE_UNLOAD = 300.0
RACE = "/api/r/{slug}"
STOP_WAIT = 30.0

log = logging.getLogger("web")


class SearchQuery(BaseModel):
    profile_id: Id
    persons: list[Id] = []
    upload: str | None = None
    box: int | Literal["whole"] = 0
    text: str | None = None
    scene: str | None = None
    start: str | None = None
    end: str | None = None
    photographers: list[str] = []
    albums: list[str] = []
    groups: list[str] = []
    bib: str | None = None
    start_bib: str | None = None
    mode: Literal["similar", "more"] = "similar"
    top: int = 60
    offset: int = 0
    seen: list[Id] = []
    exclude_photos: list[Id] = []


class NearbyQuery(BaseModel):
    profile_id: Id
    span: int = Field(2, ge=1, le=nearby.SPAN_MAX)
    anchors_bib: str | None = None
    start: str | None = None
    end: str | None = None
    photographers: list[str] = []
    albums: list[str] = []
    groups: list[str] = []
    bib: str | None = None
    hide_hidden: bool = False


class LabelBody(BaseModel):
    profile_id: Id
    person_id: Id
    label: Literal["me", "not_me"] | None = None


class BatchBody(BaseModel):
    profile_id: Id
    person_ids: list[Id]


class UndoBody(BatchBody):
    batch: str


class ProfileBody(BaseModel):
    name: str


class ExportBody(BaseModel):
    profile_id: Id


def bad(msg: str, code: int = 400) -> HTTPException:
    return HTTPException(code, msg)


def marks(values) -> str:
    return ",".join("?" * len(values))


def area(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def jpeg(img, quality=85) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def when(field: str, value: str | None, minute_end=False) -> str | None:
    try:
        return search.parse_time(value.strip() if value and value.strip() else None, minute_end)
    except ValueError as e:
        raise bad(f"{field} {e}")


def filters_of(q: SearchQuery | NearbyQuery) -> search.Filters:
    return search.Filters(when("start", q.start), when("end", q.end, minute_end=True),
                          tuple(q.photographers), tuple(q.albums), (q.bib or "").strip() or None,
                          groups=tuple(q.groups))


def photo_meta(conn, photo_ids) -> dict:
    photo_ids = list(photo_ids)
    rows = conn.execute("select id, source_photo_id, taken_at, photographer, photographer_uid, album, album_key, grp, "
                        f"width, height, relpath from photos where id in ({marks(photo_ids)})", photo_ids)
    keys = ("photo_id", "source_photo_id", "taken_at", "photographer", "photographer_uid", "album", "album_key", "group",
            "width", "height", "relpath")
    return {r[0]: dict(zip(keys, r)) for r in rows}


def site_fields(row: dict) -> dict:
    return {k: row[k] for k in ("platform", "fname", "site")}


def bibs_of(conn, person_ids) -> dict:
    out = {}
    for pid, text, conf in conn.execute("select person_id, text, conf from bibs "
                                        f"where person_id in ({marks(person_ids)}) order by conf desc, text",
                                        person_ids):
        out.setdefault(pid, []).append({"text": text, "conf": conf})
    return out


def labels_of(conn, person_ids, profile_id) -> dict:
    return dict(conn.execute(f"select person_id, label from labels where profile_id = ? "
                             f"and person_id in ({marks(person_ids)})", [profile_id, *person_ids]))


def profile_name(conn, profile_id) -> str:
    row = conn.execute("select name from profiles where id = ?", (profile_id,)).fetchone()
    if row is None:
        raise bad(f"profile {profile_id} not found", 404)
    return row[0]


def clean_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise bad("name must not be empty")
    if len(name) > NAME_MAX:
        raise bad(f"name is longer than {NAME_MAX} characters")
    return name


def check_folder(conn, name, profile_id):
    key = originals.folder_key(name)
    for other, in conn.execute("select name from profiles where id != ?", (profile_id,)):
        if originals.folder_key(other) == key:
            raise bad(f"{name!r} would share a download folder with profile {other!r}; choose another name")


def profiles_of(conn, profile_id=None) -> list[dict]:
    rows = conn.execute("select pr.id, pr.name, count(case when l.label = 'me' then 1 end), "
                        "count(case when l.label = 'not_me' then 1 end), "
                        "count(distinct case when l.label = 'me' then p.photo_id end) from profiles pr "
                        "left join labels l on l.profile_id = pr.id left join persons p on p.id = l.person_id "
                        "where ? is null or pr.id = ? group by pr.id order by pr.id", (profile_id, profile_id))
    return [dict(zip(("id", "name", "me", "not_me", "me_photos"), r)) for r in rows]


def hydrate(conn, picked, offset, profile_id, via=None) -> list[dict]:
    ids = [p for p, _ in picked]
    via = via or [None] * len(ids)
    rows = {r[0]: r[1:] for r in conn.execute(f"select id, photo_id, x1, y1, x2, y2 from persons "
                                              f"where id in ({marks(ids)})", ids)}
    photos = photo_meta(conn, {rows[p][0] for p in ids})
    bibs, labels = bibs_of(conn, ids), labels_of(conn, ids, profile_id)
    return [{"rank": offset + i, "score": s, "person_id": p, "box": list(rows[p][1:]), **photos[rows[p][0]],
             "bibs": bibs.get(p, []), "label": labels.get(p), "matched_via": v}
            for i, ((p, s), v) in enumerate(zip(picked, via), 1)]


def shot_cards(conn, shots, profile_id) -> list[dict]:
    ids = [s["person_id"] for s in shots if s["person_id"] is not None]
    boxes = {r[0]: list(r[1:]) for r in conn.execute(f"select id, x1, y1, x2, y2 from persons where id in ({marks(ids)})",
                                                     ids)}
    photos = photo_meta(conn, {s["photo_id"] for s in shots})
    bibs, labels = bibs_of(conn, ids), labels_of(conn, ids, profile_id)
    return [{"rank": None, "score": None, "person_id": s["person_id"], "box": boxes.get(s["person_id"]),
             **photos[s["photo_id"]], "bibs": bibs.get(s["person_id"], []), "label": labels.get(s["person_id"]),
             "matched_via": None, **{k: v for k, v in s.items() if k not in ("person_id", "photo_id", "taken_ts")}}
            for s in shots]


def detect_and_embed(img):
    boxes = sorted(models.detect_persons([img])[0], key=area, reverse=True)
    osnet, siglip = models.embed_crops([models.crop(img, b[:4]) for b in boxes] + [img])
    return boxes, osnet, siglip


def init_model_process(parent: int, half: bool):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    models.half_precision = half
    threading.Thread(target=watch_parent, args=(parent,), daemon=True).start()


def model_pool():
    return ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"), initializer=init_model_process,
                               initargs=(os.getpid(), models.half_precision))


class ModelWorker:
    NEEDS = {"encode_text": {"siglip"}, "detect_and_embed": {"yolo", "osnet", "siglip"}}

    def __init__(self, idle: float, factory=None, clock=time.monotonic):
        self.idle, self.factory, self.clock = idle, factory or model_pool, clock
        self.lock = threading.Lock()
        self.pool, self.ready, self.running, self.last, self.timer = None, set(), [], clock(), None
        self.stopping = None

    def needs(self, name):
        return self.NEEDS.get(name, {name})

    def submit(self, fn, *args) -> Future:
        name = fn.__name__
        with self.lock:
            if self.pool is None:
                self.pool, self.ready = self.factory(), set()
            pool, stopping = self.pool, self.stopping
            self.running.append(name)
        if stopping is not None and not stopping.wait(STOP_WAIT):
            log.warning("old model worker still exiting after %.0fs; starting the new one anyway", STOP_WAIT)
        try:
            fut = pool.submit(fn, *args)
        except BrokenProcessPool:
            with self.lock:
                self.running.remove(name)
                if self.pool is pool:
                    self.pool, self.ready = None, set()
            log.error("model worker process died while idle; starting a new one")
            return self.submit(fn, *args)
        fut.add_done_callback(lambda f: self.finished(pool, name, f))
        return fut

    def finished(self, pool, name, fut):
        broken = not fut.cancelled() and isinstance(fut.exception(), BrokenProcessPool)
        with self.lock:
            self.running.remove(name)
            self.last = self.clock()
            if pool is not self.pool:
                return
            if broken:
                log.error("model worker process died; the next request starts a new one")
                self.pool, self.ready = None, set()
                return
            if not fut.cancelled() and fut.exception() is None:
                self.ready |= self.needs(name)
            if self.timer is None:
                self.arm(self.idle)

    def arm(self, delay):
        self.timer = threading.Timer(delay, self.check)
        self.timer.daemon = True
        self.timer.start()

    def check(self):
        with self.lock:
            self.timer = None
            if self.pool is None or self.running:
                return
            left = self.last + self.idle - self.clock()
            if left > 0:
                self.arm(left)
                return
            pool, self.pool, self.ready = self.pool, None, set()
            stopping = self.stopping = threading.Event()
        log.info("no model work for %.0fs; stopping the model worker process", self.idle)
        try:
            pool.shutdown(wait=True)
        finally:
            with self.lock:
                if self.stopping is stopping:
                    self.stopping = None
            stopping.set()

    def status(self) -> dict:
        with self.lock:
            loading = [n for n in self.running if self.needs(n) - self.ready]
            return {"running": self.pool is not None, "ready": sorted(self.ready),
                    "loading": loading[0] if loading else None, "unload_after_s": self.idle}


@dataclass(eq=False)
class RaceState:
    slug: str
    name: str
    dir: Path
    persons: search.Persons
    scene_error: str | None
    has_originals: bool
    job: originals.Job
    uploads: OrderedDict = field(default_factory=OrderedDict)


def open_race(slug: str, name: str, directory: Path, fetcher: originals.Fetcher, hint: str | None = None) -> RaceState:
    directory = directory.resolve()
    with closing(db.connect(directory)) as conn:
        try:
            persons = search.load_persons(conn)
        except search.MissingEmbeddings as e:
            raise search.MissingEmbeddings(str(e).replace("<collection>", hint or slug)) from None
        try:
            search.load_scenes(conn, persons)
            scene_error = None
        except search.MissingEmbeddings as e:
            scene_error = str(e)
    return RaceState(slug, name, directory, persons, scene_error, originals.has_originals(directory),
                     originals.Job(fetcher))


def path_race(directory: Path) -> races.Race:
    slug = directory.name
    if races.race_dir(slug).resolve() == directory and (found := races.load().race(slug)):
        return found
    return races.Race(slug, slug)


PERSONS_READY = ("select 1 from persons p join photos ph on ph.id = p.photo_id "
                 "join emb_person_osnet o on o.person_id = p.id join emb_person_siglip s on s.person_id = p.id "
                 "where ph.status = 'ok' limit 1")


def embedded(directory: Path) -> bool:
    index = directory / db.INDEX_NAME
    try:
        if not index.is_file():
            return False
        with closing(sqlite3.connect(f"{index.resolve().as_uri()}?mode=ro", uri=True, timeout=2)) as conn:
            return conn.execute(PERSONS_READY).fetchone() is not None
    except (sqlite3.Error, OSError):
        return False


def downloaded(manifest: Path) -> int:
    try:
        if not manifest.is_file():
            return 0
        with closing(sqlite3.connect(f"{manifest.resolve().as_uri()}?mode=ro", uri=True, timeout=2)) as conn:
            view = conn.execute("select 1 from sqlite_master where name = 'catalog'").fetchone()
            return conn.execute("select count(*) from " + ("catalog" if view else f"({CATALOG_SELECT})")
                                + " where status = 'done'").fetchone()[0]
    except (sqlite3.Error, OSError):
        return 0


class Stale(Exception):
    def __init__(self, loaded: RaceState | None):
        self.loaded = loaded


def create_app(collection: Path | None = None, *, registry: Callable[[], races.Registry] | None = None,
               fetcher: originals.Fetcher | None = None, idle_unload: float = IDLE_UNLOAD,
               worker_factory=None, load: str | None = None) -> FastAPI:
    fetcher = fetcher or originals.Fetcher()
    worker = ModelWorker(idle_unload, worker_factory)
    load_lock = threading.Lock()
    app = FastAPI(title="photofinder")
    app.state.models = worker
    app.state.current = app.state.originals = None
    only = None

    def set_current(st: RaceState | None):
        app.state.current, app.state.originals = st, st and st.job

    if collection is not None:
        collection = collection.resolve()
        only = path_race(collection)
        registered = races.race_dir(only.slug).resolve() == collection
        set_current(open_race(only.slug, only.name, collection, fetcher, None if registered else str(collection)))

    def listing() -> list[tuple[races.Race, Path]]:
        if only is not None:
            return [(only, collection)]
        return [(r, races.race_dir(r.slug)) for r in (registry or races.load)().races]

    if load is not None:
        race, d = next((race, d) for race, d in listing() if race.slug == load)
        set_current(open_race(load, race.name, d, fetcher))

    async def bound(slug: str) -> RaceState:
        st = app.state.current
        if st is None or st.slug != slug:
            raise Stale(st)
        return st

    Race = Annotated[RaceState, Depends(bound)]

    def connect(st: RaceState):
        return closing(db.connect(st.dir))

    async def on_models(fn, *args):
        try:
            return await asyncio.wrap_future(await run_in_threadpool(worker.submit, fn, *args))
        except BrokenProcessPool:
            raise bad("the model worker stopped, probably because the Mac ran low on memory; try again", 503)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        e = exc.errors()[0]
        field = ".".join(str(x) for x in e["loc"][1:]) or "request"
        return JSONResponse({"detail": f"{field}: {e['msg']}"}, 400)

    @app.exception_handler(search.MissingEmbeddings)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc)}, 400)

    @app.exception_handler(Stale)
    async def stale(request, exc):
        st = exc.loaded
        detail = f"The server switched to {st.name} — reload" if st else "No race is loaded on the server — reload"
        return JSONResponse({"detail": detail, "loaded": st and st.slug}, 409)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/models")
    def model_status():
        return worker.status()

    @app.get("/api/races")
    def list_races():
        st = app.state.current
        return [{"slug": race.slug, "name": race.name, "indexed": embedded(d),
                 "loaded": st is not None and st.slug == race.slug,
                 "albums": [{"key": a.key, "platform": a.platform, "title": a.title, "url": a.url,
                             "downloaded": downloaded(d / ALBUMS / a.key / MANIFEST_NAME)} for a in race.albums]}
                for race, d in listing()]

    @app.post("/api/races/{slug}/load")
    def load_race(slug: str):
        if not load_lock.acquire(blocking=False):
            raise bad("another race is loading; try again when it finishes", 409)
        try:
            race, d = next(((race, d) for race, d in listing() if race.slug == slug), (None, None))
            if race is None:
                raise bad(f"no race {slug!r}", 404)
            old = app.state.current
            if old is not None and old.slug == slug:
                return facets_of(old)
            if not embedded(d):
                raise bad(f"{race.name} is not indexed yet (no person embeddings); "
                          f"run `photofinder index {slug}` first")
            if old is not None:
                if not old.job.busy.acquire(blocking=False):
                    if old.job.status()["state"] == "running":
                        raise bad(f"an originals download is running for {old.name}; "
                                  "cancel it or wait until it finishes before switching races", 409)
                    raise bad("a download is in progress; try again in a moment", 409)
                set_current(None)
                del old
            st = open_race(slug, race.name, d, fetcher)
            set_current(st)
            return facets_of(st)
        finally:
            load_lock.release()

    def facets_of(st: RaceState, profile_id=None):
        with connect(st) as conn:
            if profile_id is not None:
                profile_name(conn, profile_id)
            count, first, last = conn.execute("select count(*), min(taken_at), max(taken_at) from photos "
                                              "where status = 'ok'").fetchone()
            photographers = [{"name": n, "uid": u, "photos": c} for n, u, c in conn.execute(
                "select photographer, photographer_uid, count(*) from photos where status = 'ok' "
                "and coalesce(photographer, photographer_uid) is not null "
                "group by photographer_uid, photographer order by count(*) desc, photographer is null, photographer")]
            albums = [{"name": a, "photos": c} for a, c in conn.execute(
                "select album, count(*) from photos where status = 'ok' and album is not null "
                "group by album order by count(*) desc, album")]
            groups = [{"name": g, "photos": c} for g, c in conn.execute(
                "select grp, count(*) from photos where status = 'ok' and grp is not null "
                "group by grp order by count(*) desc, grp")]
            labels = dict(conn.execute("select label, count(*) from labels where profile_id = ? group by label",
                                       (profile_id,)))
            warnings = [st.scene_error or search.check_scenes(conn)]
            try:
                warnings.append(search.check_filters(conn, search.Filters(bib="facets")))
            except search.MissingEmbeddings as e:
                warnings.append(str(e))
        out = {"race": {"slug": st.slug, "name": st.name}, "collection": st.dir.name, "photos": count,
               "persons": len(st.persons.ids), "taken_at": {"min": first, "max": last}, "photographers": photographers,
               "albums": albums, "groups": groups,
               "scenes": st.scene_error is None, "originals": st.has_originals, "warnings": [w for w in warnings if w]}
        if profile_id is not None:
            out["labels"] = {"me": labels.get("me", 0), "not_me": labels.get("not_me", 0)}
        return out

    @app.get(RACE + "/facets")
    def facets(st: Race, profile_id: Id | None = None):
        return facets_of(st, profile_id)

    def downloading(st: RaceState, profile_id):
        status = st.job.status()
        if status["state"] == "running" and status["profile_id"] == profile_id:
            raise bad("an originals download is running for this profile; cancel it or wait until it finishes", 409)

    @app.get(RACE + "/profiles")
    def list_profiles(st: Race):
        with connect(st) as conn:
            return {"profiles": profiles_of(conn)}

    @app.post(RACE + "/profiles")
    def create_profile(st: Race, body: ProfileBody):
        name = clean_name(body.name)
        with connect(st) as conn, conn:
            try:
                pid = conn.execute("insert into profiles(name, created_at) values (?, ?)", (name, now())).lastrowid
            except sqlite3.IntegrityError:
                raise bad(f"a profile named {name!r} already exists")
            check_folder(conn, name, pid)
            return profiles_of(conn, pid)[0]

    @app.patch(RACE + "/profiles/{profile_id}")
    def rename_profile(st: Race, profile_id: Id, body: ProfileBody):
        name = clean_name(body.name)
        with connect(st) as conn, conn:
            old = originals.profile_folder(st.dir, profile_name(conn, profile_id))
            downloading(st, profile_id)
            try:
                conn.execute("update profiles set name = ? where id = ?", (name, profile_id))
            except sqlite3.IntegrityError:
                raise bad(f"a profile named {name!r} already exists")
            check_folder(conn, name, profile_id)
            new = originals.profile_folder(st.dir, name)
            if old != new:
                try:
                    st.job.claim()
                except originals.Busy:
                    raise bad("a download is in progress; rename when it finishes", 409)
                try:
                    moved = old.exists()
                    if moved:
                        if old.name.casefold() != new.name.casefold() and new.exists():
                            raise bad(f"folder {new} already exists; move or delete it first")
                        old.rename(new)
                    try:
                        if moved and (new / originals.CSV_NAME).is_file():
                            originals.write_csv(new, marked(st, conn, profile_id))
                        conn.commit()
                    except Exception:
                        if moved:
                            new.rename(old)
                        raise
                finally:
                    st.job.busy.release()
            return profiles_of(conn, profile_id)[0]

    @app.delete(RACE + "/profiles/{profile_id}")
    def delete_profile(st: Race, profile_id: Id):
        with connect(st) as conn, conn:
            conn.execute("begin immediate")
            profile_name(conn, profile_id)
            downloading(st, profile_id)
            if conn.execute("select count(*) from profiles").fetchone()[0] == 1:
                raise bad("cannot delete the last profile; create another one first")
            n = conn.execute("delete from labels where profile_id = ?", (profile_id,)).rowcount
            conn.execute("delete from profiles where id = ?", (profile_id,))
        return {"id": profile_id, "labels_removed": n}

    @app.post(RACE + "/upload")
    async def upload(st: Race, file: UploadFile = File(...)):
        data = await file.read()
        try:
            img = await run_in_threadpool(models.load_image, io.BytesIO(data))
        except Exception:
            raise bad("not an image; upload a JPEG or PNG photo")
        boxes, osnet, siglip = await on_models(detect_and_embed, img)
        token = secrets.token_urlsafe(9)
        st.uploads[token] = {"jpeg": await run_in_threadpool(jpeg, img, 90), "size": img.size, "boxes": boxes,
                             "osnet": osnet, "siglip": siglip}
        while len(st.uploads) > UPLOADS:
            st.uploads.popitem(last=False)
        return {"token": token, "width": img.width, "height": img.height,
                "boxes": [{"index": i, "box": list(b[:4]), "conf": b[4]} for i, b in enumerate(boxes)]}

    def get_upload(st: RaceState, token):
        up = st.uploads.get(token)
        if up is None:
            raise bad("upload expired or unknown; upload the photo again", 404)
        return up

    @app.get(RACE + "/uploads/{token}/image")
    def upload_image(st: Race, token: str):
        return Response(get_upload(st, token)["jpeg"], media_type="image/jpeg", headers=CACHE)

    def prepare(st: RaceState, q: SearchQuery):
        persons = st.persons
        with connect(st) as conn:
            profile_name(conn, q.profile_id)
            labels = conn.execute("select l.person_id, l.label, p.photo_id, l.hidden from labels l "
                                  "join persons p on p.id = l.person_id where l.profile_id = ?",
                                  (q.profile_id,)).fetchall()
        embedded = lambda ids: [p for p, ok in zip(ids, np.isin(ids, persons.ids)) if ok]
        me = [p for p, label, *_ in labels if label == "me"]
        not_me = [p for p, label, *_ in labels if label == "not_me"]
        me_emb, not_me_emb = embedded(me), embedded(not_me)
        if q.mode == "more":
            if not me:
                raise bad("no person is marked as me yet; mark at least one person as me first")
            if not me_emb:
                raise bad("the people marked as me have no embeddings yet; rerun `photofinder index`")
        skipped = len(not_me) - len(not_me_emb) + (len(me) - len(me_emb) if q.mode == "more" else 0)
        note = f"{skipped} marked {'person has' if skipped == 1 else 'people have'} no embeddings yet; " \
               "rerun `photofinder index`" if skipped else None
        unknown = sorted(set(q.persons) - set(embedded(q.persons)))
        if unknown:
            raise bad(f"person {unknown[0]} is not an indexed person with embeddings")
        ids = list(q.persons) + (me_emb if q.mode == "more" else [])
        refs = search.person_refs(persons, ids) if ids else {}
        ref_ids = persons.ids[np.isin(persons.ids, ids)] if ids else []
        if q.upload:
            up = get_upload(st, q.upload)
            row = len(up["boxes"]) if q.box == "whole" else q.box
            if q.box != "whole" and not 0 <= row < len(up["boxes"]):
                raise bad(f"box {q.box} out of range; valid boxes are 0..{len(up['boxes']) - 1} or whole"
                          if up["boxes"] else f"box {q.box}: no person detected in the upload; use whole")
            for k in ("osnet", "siglip"):
                v = up[k][row:row + 1]
                refs[k] = np.vstack([refs[k], v]) if k in refs else v
        negatives = search.person_refs(persons, not_me_emb)["osnet"] if not_me_emb else None
        exclude = {photo for _, label, photo, _ in labels if label == "me"} if q.mode == "more" else set()
        hidden = {photo for _, label, photo, flag in labels if label == "not_me" and flag} - exclude \
            if q.mode == "more" else set()
        drop = not_me if q.mode == "more" else []
        return refs, ref_ids, negatives, exclude | hidden | set(q.seen) | set(q.exclude_photos), drop, note, hidden

    def ranked(st: RaceState, q, filters, top, offset, refs, ref_ids, negatives, exclude, drop, note, hidden):
        persons = st.persons
        with connect(st) as conn:
            warnings = [note, search.check_filters(conn, filters)]
            if "scene" in refs:
                warnings.append(search.check_scenes(conn))
            mask = search.filter_mask(conn, persons, filters)
            t0 = time.monotonic()
            scores = search.score(persons, refs, negatives=negatives)
            keep = np.ones(len(scores), bool) if mask is None else mask
            shown = set(persons.photo_ids[keep].tolist())
            if drop:
                keep &= ~np.isin(persons.ids, drop)
            rows = np.flatnonzero(keep)
            picked = [int(rows[i]) for i in
                      search.best_per_photo(persons.photo_ids[rows], scores[rows], offset + top, exclude)][offset:]
            timing = time.monotonic() - t0
            via = search.matched_via(persons, picked, refs, ref_ids)
            results = hydrate(conn, [(int(persons.ids[i]), float(scores[i])) for i in picked],
                              offset + len(set(q.seen)), q.profile_id, via)
        out = {"results": results, "warnings": [w for w in warnings if w], "timing": round(timing, 4)}
        if q.mode == "more":
            out["hidden"] = len(hidden & shown)
        return out

    def bib_start(st: RaceState, q, filters, top, offset):
        bib = q.start_bib.strip()
        if not bib:
            raise bad("start_bib needs a bib number, e.g. 8038")
        if q.persons or q.upload or (q.text or "").strip() or (q.scene or "").strip() or q.mode == "more":
            raise bad("start_bib cannot be combined with persons, upload, text, scene or find-more")
        with connect(st) as conn:
            profile_name(conn, q.profile_id)
            warnings = [search.check_filters(conn, search.Filters(bib=bib))]
            where, args = search.filter_where(filters)
            t0 = time.monotonic()
            rows = conn.execute("select p.id, p.photo_id from persons p join photos ph on ph.id = p.photo_id "
                                f"join bibs b on b.person_id = p.id where b.text = ? and {' and '.join(where)} "
                                f"{FIRST}, b.conf desc, p.id", [bib, *args])
            seen, picked = set(), []
            for pid, photo in rows:
                if photo not in seen:
                    seen.add(photo)
                    picked.append((pid, None))
            timing = time.monotonic() - t0
            results = hydrate(conn, picked[offset:offset + top], offset, q.profile_id)
        return {"results": results, "total": len(picked), "warnings": [w for w in warnings if w],
                "timing": round(timing, 4)}

    @app.post(RACE + "/search")
    async def search_photos(st: Race, q: SearchQuery):
        filters = filters_of(q)
        top, offset = min(max(q.top, 1), MAX_TOP), max(q.offset, 0)
        if q.start_bib is not None:
            return await run_in_threadpool(bib_start, st, q, filters, top, offset)
        texts = {k: v.strip() for k, v in (("text", q.text), ("scene", q.scene)) if v and v.strip()}
        if "scene" in texts and st.scene_error:
            raise bad(st.scene_error)
        refs, ref_ids, negatives, exclude, drop, note, hidden = await run_in_threadpool(prepare, st, q)
        if not refs and not texts:
            raise bad("give a person, an uploaded box, text, scene or start_bib to search")
        if texts:
            vecs = await on_models(models.encode_text, list(texts.values()))
            refs.update({k: v[None] for k, v in zip(texts, vecs)})
        return await run_in_threadpool(ranked, st, q, filters, top, offset, refs, ref_ids, negatives, exclude, drop,
                                       note, hidden)

    @app.post(RACE + "/labels")
    def set_label(st: Race, body: LabelBody):
        with connect(st) as conn, conn:
            conn.execute("begin immediate")
            profile_name(conn, body.profile_id)
            if not conn.execute("select 1 from persons where id = ?", (body.person_id,)).fetchone():
                raise bad(f"person {body.person_id} not found", 404)
            key = (body.profile_id, body.person_id)
            previous = conn.execute("select label from labels where profile_id = ? and person_id = ?", key).fetchone()
            if body.label is None:
                conn.execute("delete from labels where profile_id = ? and person_id = ?", key)
            else:
                conn.execute("insert into labels(profile_id, person_id, label, created_at) values (?, ?, ?, ?) "
                             "on conflict(profile_id, person_id) "
                             "do update set label = excluded.label, created_at = excluded.created_at, hidden = 0",
                             (*key, body.label, now()))
        return {"profile_id": body.profile_id, "person_id": body.person_id, "label": body.label,
                "previous": previous and previous[0]}

    @app.post(RACE + "/labels/batch")
    def hide_not_me(st: Race, body: BatchBody):
        ids = list(dict.fromkeys(body.person_ids))
        with connect(st) as conn, conn:
            conn.execute("begin immediate")
            profile_name(conn, body.profile_id)
            photo_of = dict(conn.execute(f"select id, photo_id from persons where id in ({marks(ids)})", ids))
            missing = [p for p in ids if p not in photo_of]
            if missing:
                raise bad(f"person {missing[0]} not found", 404)
            label_of = dict(conn.execute(f"select person_id, label from labels where profile_id = ? "
                                         f"and person_id in ({marks(ids)})", [body.profile_id, *ids]))
            mine = {p for p, in conn.execute("select p.photo_id from labels l join persons p on p.id = l.person_id "
                                             "where l.profile_id = ? and l.label = 'me'", (body.profile_id,))}
            changed, skipped = [], []
            for p in ids:
                if p in label_of:
                    skipped.append({"person_id": p, "reason": f"already {label_of[p]}"})
                elif photo_of[p] in mine:
                    skipped.append({"person_id": p, "reason": "photo has a me person"})
                else:
                    changed.append(p)
            stamp = datetime.now().isoformat(sep=" ", timespec="microseconds")
            conn.executemany("insert into labels(profile_id, person_id, label, created_at, hidden) "
                             "values (?, ?, 'not_me', ?, 1)", [(body.profile_id, p, stamp) for p in changed])
        return {"profile_id": body.profile_id, "batch": stamp, "changed": changed, "skipped": skipped}

    @app.post(RACE + "/labels/batch/undo")
    def unhide_not_me(st: Race, body: UndoBody):
        ids = list(dict.fromkeys(body.person_ids))
        with connect(st) as conn, conn:
            conn.execute("begin immediate")
            profile_name(conn, body.profile_id)
            still = [p for p, in conn.execute(f"select person_id from labels where profile_id = ? and label = 'not_me' "
                                              f"and hidden = 1 and created_at = ? and person_id in ({marks(ids)})",
                                              [body.profile_id, body.batch, *ids])]
            conn.executemany("delete from labels where profile_id = ? and person_id = ?",
                             [(body.profile_id, p) for p in still])
        return {"profile_id": body.profile_id, "removed": still,
                "kept": [p for p in ids if p not in set(still)]}

    @app.get(RACE + "/photos/{photo_id}")
    def photo(st: Race, photo_id: Id, profile_id: Id):
        with connect(st) as conn:
            profile_name(conn, profile_id)
            meta = photo_meta(conn, [photo_id]).get(photo_id)
            if meta is None:
                raise bad(f"photo {photo_id} not found", 404)
            rows = conn.execute("select id, x1, y1, x2, y2 from persons where photo_id = ? order by id",
                                (photo_id,)).fetchall()
            ids = [r[0] for r in rows]
            bibs, labels = bibs_of(conn, ids), labels_of(conn, ids, profile_id)
        return {**meta, **site_fields(originals.sites_of(st.dir, [meta])[0]), "persons": [{"person_id": r[0], "box": list(r[1:]), "bibs": bibs.get(r[0], []),
                                     "label": labels.get(r[0])} for r in rows]}

    def photo_neighbors(st: RaceState, photo_id, profile_id, span, person_id):
        persons = st.persons
        with connect(st) as conn:
            profile_name(conn, profile_id)
            anchor = photo_meta(conn, [photo_id]).get(photo_id)
            if anchor is None:
                raise bad(f"photo {photo_id} not found", 404)
            mine = dict(conn.execute("select p.id, l.label from persons p left join labels l on l.person_id = p.id "
                                     "and l.profile_id = ? where p.photo_id = ?", (profile_id, photo_id)))
            if person_id is not None:
                if person_id not in mine:
                    raise bad(f"person {person_id} is not in photo {photo_id}")
                ref_ids = [person_id]
                if not nearby.rows_of(persons, ref_ids):
                    raise bad(f"person {person_id} is not an indexed person with embeddings")
            else:
                ref_ids = sorted(p for p, label in mine.items() if label == "me")
            refs = nearby.rows_of(persons, ref_ids)
            not_me = {p for p, label in conn.execute("select person_id, label from labels where profile_id = ?",
                                                     (profile_id,)) if label == "not_me"}
            rolls, reasons = nearby.rolls(conn, [photo_id], span)
            shots = rolls.get(photo_id, [])
            by_photo = nearby.rows_by_photo(persons, [p for p, *_ in shots])
            found = []
            for photo, offset, gap, _ in shots:
                rows = [r for r in by_photo.get(photo, []) if int(persons.ids[r]) not in not_me]
                pid, _, sim = nearby.best_match(persons, rows, refs) or (None, None, None)
                found.append(nearby.shot(photo, offset, gap, person_id=pid, similarity=sim))
            cards = shot_cards(conn, found, profile_id)
        return {"anchor": anchor, "span": span, "reference": [int(persons.ids[r]) for r in refs],
                "confirmed": any(mine.get(int(persons.ids[r])) == "me" for r in refs),
                "neighbors": cards, "reason": reasons.get(photo_id)}

    @app.get(RACE + "/photos/{photo_id}/neighbors")
    async def neighbors(st: Race, photo_id: Id, profile_id: Id, span: int = Query(3, ge=1, le=nearby.SPAN_MAX),
                        person_id: Id | None = None):
        return await run_in_threadpool(photo_neighbors, st, photo_id, profile_id, span, person_id)

    def nearby_shots(st: RaceState, q: NearbyQuery, filters):
        bib = (q.anchors_bib or "").strip() or None
        with connect(st) as conn:
            profile_name(conn, q.profile_id)
            warnings = [search.check_filters(conn, filters)]
            if bib:
                try:
                    warnings.append(search.check_filters(conn, search.Filters(bib=bib)))
                except search.MissingEmbeddings as e:
                    warnings.append(str(e))
            t0 = time.monotonic()
            shots, notes = nearby.collect(conn, st.persons, q.profile_id, q.span, filters, bib, q.hide_hidden)
            timing = time.monotonic() - t0
            results = shot_cards(conn, shots, q.profile_id)
        return {"results": results, "count": len(results), "warnings": [w for w in warnings + notes if w],
                "timing": round(timing, 4)}

    @app.post(RACE + "/nearby")
    async def nearby_photos(st: Race, q: NearbyQuery):
        return await run_in_threadpool(nearby_shots, st, q, filters_of(q))

    def photo_path(st: RaceState, photo_id) -> Path:
        with connect(st) as conn:
            row = conn.execute("select relpath from photos where id = ?", (photo_id,)).fetchone()
        if row is None:
            raise bad(f"photo {photo_id} not found", 404)
        return st.dir / row[0]

    def open_photo(photo_id, path):
        try:
            return models.load_image(path)
        except Exception:
            raise bad(f"photo {photo_id} file is missing or unreadable", 404)

    @app.get(RACE + "/photos/{photo_id}/image")
    def photo_image(st: Race, photo_id: Id, size: int | None = Query(None, alias="max", ge=16, le=8192)):
        path = photo_path(st, photo_id)
        if size is None:
            try:
                with open(path, "rb"):
                    pass
            except OSError:
                raise bad(f"photo {photo_id} file is missing or unreadable", 404)
            return FileResponse(path, headers=CACHE)
        img = open_photo(photo_id, path)
        img.thumbnail((size, size))
        return Response(jpeg(img), media_type="image/jpeg", headers=CACHE)

    @app.get(RACE + "/persons/{person_id}/crop")
    def person_crop(st: Race, person_id: Id):
        with connect(st) as conn:
            row = conn.execute("select ph.id, ph.relpath, p.x1, p.y1, p.x2, p.y2 from persons p "
                               "join photos ph on ph.id = p.photo_id where p.id = ?", (person_id,)).fetchone()
        if row is None:
            raise bad(f"person {person_id} not found", 404)
        photo_id, relpath, x1, y1, x2, y2 = row
        img = open_photo(photo_id, st.dir / relpath)
        pw, ph = (x2 - x1) * CROP_PAD, (y2 - y1) * CROP_PAD
        img = models.crop(img, (x1 - pw, y1 - ph, x2 + pw, y2 + ph))
        if img.height > CROP_HEIGHT:
            img = img.resize((max(1, round(img.width * CROP_HEIGHT / img.height)), CROP_HEIGHT))
        return Response(jpeg(img), media_type="image/jpeg", headers=CACHE)

    def me_rows(conn, profile_id):
        return conn.execute("select ph.id, p.id, p.x1, p.y1, p.x2, p.y2 from labels l "
                            "join persons p on p.id = l.person_id join photos ph on ph.id = p.photo_id "
                            f"where l.profile_id = ? and l.label = 'me' {FIRST}, p.id", (profile_id,)).fetchall()

    def marked(st: RaceState, conn, profile_id):
        ids = list(dict.fromkeys(r[0] for r in me_rows(conn, profile_id)))
        meta = photo_meta(conn, ids)
        return originals.rows_of(st.dir, [meta[i] for i in ids])

    @app.get(RACE + "/me")
    def my_photos(st: Race, profile_id: Id):
        with connect(st) as conn:
            name = profile_name(conn, profile_id)
            rows = me_rows(conn, profile_id)
            meta = photo_meta(conn, {r[0] for r in rows})
            bibs = bibs_of(conn, [r[1] for r in rows])
        found = originals.rows_of(st.dir, list(meta.values()))
        status = originals.statuses(originals.profile_folder(st.dir, name), found)
        sites = {r["photo_id"]: site_fields(r) for r in found}
        photos = {}
        for photo_id, pid, *box in rows:
            photos.setdefault(photo_id, {**meta[photo_id], **sites[photo_id], "original": status[photo_id],
                                         "persons": []})["persons"].append(
                {"person_id": pid, "box": box, "bibs": bibs.get(pid, []), "label": "me"})
        return {"photos": list(photos.values()), "count": len(photos)}

    @app.post(RACE + "/export")
    def export(st: Race, body: ExportBody):
        with connect(st) as conn:
            name = profile_name(conn, body.profile_id)
            rows = marked(st, conn, body.profile_id)
        if not rows:
            raise bad("no photos marked as me yet; nothing to export")
        return {"path": str(originals.write_csv(originals.profile_folder(st.dir, name), rows)), "count": len(rows)}

    def busy(st: RaceState):
        if st.job.status()["state"] == "running":
            return bad("an originals download is already running", 409)
        return bad("a download is in progress; try again in a moment", 409)

    def need_originals(st: RaceState):
        if not st.has_originals:
            raise bad("originals are only available for yipai360 and photoplus collections (no matching manifest)")

    @app.post(RACE + "/originals")
    def start_originals(st: Race, body: ExportBody):
        need_originals(st)
        try:
            st.job.claim()
        except originals.Busy:
            raise busy(st)
        try:
            with connect(st) as conn:
                name = profile_name(conn, body.profile_id)
                rows = marked(st, conn, body.profile_id)
            if not rows:
                raise bad("no photos marked as me yet; nothing to download")
            st.job.start(body.profile_id, name, originals.profile_folder(st.dir, name), rows)
        except BaseException:
            st.job.busy.release()
            raise
        return st.job.status()

    @app.get(RACE + "/originals")
    def originals_status(st: Race):
        return st.job.status()

    @app.post(RACE + "/originals/cancel")
    def cancel_originals(st: Race):
        st.job.cancel()
        return st.job.status()

    @app.get(RACE + "/originals/zip")
    def originals_zip(st: Race, profile_id: Id):
        need_originals(st)
        with connect(st) as conn:
            name = profile_name(conn, profile_id)
        folder = originals.profile_folder(st.dir, name)
        path = originals.zip_folder(folder)
        if path is None:
            raise bad(f"no originals downloaded yet for {name}", 404)
        return FileResponse(path, media_type="application/zip", filename=f"{st.dir.name}-{folder.name}-originals.zip",
                            background=BackgroundTask(path.unlink, missing_ok=True))

    @app.post(RACE + "/photos/{photo_id}/original")
    def photo_original(st: Race, photo_id: Id, body: ExportBody):
        need_originals(st)
        try:
            with st.job.claimed():
                with connect(st) as conn:
                    name = profile_name(conn, body.profile_id)
                    meta = photo_meta(conn, [photo_id]).get(photo_id)
                    if meta is None:
                        raise bad(f"photo {photo_id} not found", 404)
                    rows = marked(st, conn, body.profile_id)
                row = originals.rows_of(st.dir, [meta])[0]
                result, dest = st.job.single(row, originals.profile_folder(st.dir, name), rows)
        except originals.Busy:
            raise busy(st)
        except originals.Cancelled:
            raise bad("download was cancelled; try again", 409)
        except originals.Unavailable as e:
            raise bad(str(e), 502)
        if result == originals.OPEN_ON_SITE:
            raise bad(f"{result}: originals are downloaded only from yipai360 and photoplus; open this photo on "
                      f"{row['platform']} instead", 409)
        if result.startswith("buy on site"):
            raise bad(result, 402)
        if result != originals.DOWNLOADED:
            raise bad(result, 502)
        return FileResponse(dest, media_type="image/jpeg", filename=dest.name)

    return app
