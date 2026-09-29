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
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from photofinder import db, models, originals, search
from photofinder.sources.yipai import Blocked
from photofinder.index.stages import now

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


class LabelBody(BaseModel):
    profile_id: Id
    person_id: Id
    label: Literal["me", "not_me"] | None = None


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


def filters_of(q: SearchQuery) -> search.Filters:
    return search.Filters(when("start", q.start), when("end", q.end, minute_end=True),
                          tuple(q.photographers), tuple(q.albums), (q.bib or "").strip() or None,
                          groups=tuple(q.groups))


def photo_meta(conn, photo_ids) -> dict:
    photo_ids = list(photo_ids)
    rows = conn.execute("select id, source_photo_id, taken_at, photographer, photographer_uid, album, grp, width, "
                        f"height, relpath from photos where id in ({marks(photo_ids)})", photo_ids)
    keys = ("photo_id", "source_photo_id", "taken_at", "photographer", "photographer_uid", "album", "grp", "width",
            "height", "relpath")
    return {r[0]: dict(zip(keys, r)) for r in rows}


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


def detect_and_embed(img):
    boxes = sorted(models.detect_persons([img])[0], key=area, reverse=True)
    osnet, siglip = models.embed_crops([models.crop(img, b[:4]) for b in boxes] + [img])
    return boxes, osnet, siglip


def watch_parent(parent: int):
    while os.getppid() == parent:
        time.sleep(2)
    os._exit(0)


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


def create_app(collection: Path, fetcher: originals.Fetcher | None = None, idle_unload: float = IDLE_UNLOAD,
               worker_factory=None) -> FastAPI:
    collection = collection.resolve()
    yipai = originals.is_yipai(collection)
    job = originals.Job(fetcher or originals.Fetcher())
    with closing(db.connect(collection)) as conn:
        persons = search.load_persons(conn)
        try:
            search.load_scenes(conn, persons)
            scene_error = None
        except search.MissingEmbeddings as e:
            scene_error = str(e)
    worker = ModelWorker(idle_unload, worker_factory)
    uploads = OrderedDict()
    app = FastAPI(title="photofinder")
    app.state.originals = job
    app.state.models = worker

    def connect():
        return closing(db.connect(collection))

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

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/facets")
    def facets(profile_id: Id | None = None):
        with connect() as conn:
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
            warnings = [scene_error or search.check_scenes(conn)]
            try:
                warnings.append(search.check_filters(conn, search.Filters(bib="facets")))
            except search.MissingEmbeddings as e:
                warnings.append(str(e))
        out = {"collection": collection.name, "photos": count, "persons": len(persons.ids),
               "taken_at": {"min": first, "max": last}, "photographers": photographers, "albums": albums,
               "groups": groups,
               "scenes": scene_error is None, "originals": yipai, "warnings": [w for w in warnings if w]}
        if profile_id is not None:
            out["labels"] = {"me": labels.get("me", 0), "not_me": labels.get("not_me", 0)}
        return out

    @app.get("/api/models")
    def model_status():
        return worker.status()

    def downloading(profile_id):
        status = job.status()
        if status["state"] == "running" and status["profile_id"] == profile_id:
            raise bad("an originals download is running for this profile; cancel it or wait until it finishes", 409)

    @app.get("/api/profiles")
    def list_profiles():
        with connect() as conn:
            return {"profiles": profiles_of(conn)}

    @app.post("/api/profiles")
    def create_profile(body: ProfileBody):
        name = clean_name(body.name)
        with connect() as conn, conn:
            try:
                pid = conn.execute("insert into profiles(name, created_at) values (?, ?)", (name, now())).lastrowid
            except sqlite3.IntegrityError:
                raise bad(f"a profile named {name!r} already exists")
            check_folder(conn, name, pid)
            return profiles_of(conn, pid)[0]

    @app.patch("/api/profiles/{profile_id}")
    def rename_profile(profile_id: Id, body: ProfileBody):
        name = clean_name(body.name)
        with connect() as conn, conn:
            old = originals.profile_folder(collection, profile_name(conn, profile_id))
            downloading(profile_id)
            try:
                conn.execute("update profiles set name = ? where id = ?", (name, profile_id))
            except sqlite3.IntegrityError:
                raise bad(f"a profile named {name!r} already exists")
            check_folder(conn, name, profile_id)
            new = originals.profile_folder(collection, name)
            if old != new:
                try:
                    job.claim()
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
                            originals.write_csv(new, marked(conn, profile_id))
                        conn.commit()
                    except Exception:
                        if moved:
                            new.rename(old)
                        raise
                finally:
                    job.busy.release()
            return profiles_of(conn, profile_id)[0]

    @app.delete("/api/profiles/{profile_id}")
    def delete_profile(profile_id: Id):
        with connect() as conn, conn:
            conn.execute("begin immediate")
            profile_name(conn, profile_id)
            downloading(profile_id)
            if conn.execute("select count(*) from profiles").fetchone()[0] == 1:
                raise bad("cannot delete the last profile; create another one first")
            n = conn.execute("delete from labels where profile_id = ?", (profile_id,)).rowcount
            conn.execute("delete from profiles where id = ?", (profile_id,))
        return {"id": profile_id, "labels_removed": n}

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...)):
        data = await file.read()
        try:
            img = await run_in_threadpool(models.load_image, io.BytesIO(data))
        except Exception:
            raise bad("not an image; upload a JPEG or PNG photo")
        boxes, osnet, siglip = await on_models(detect_and_embed, img)
        token = secrets.token_urlsafe(9)
        uploads[token] = {"jpeg": await run_in_threadpool(jpeg, img, 90), "size": img.size, "boxes": boxes,
                          "osnet": osnet, "siglip": siglip}
        while len(uploads) > UPLOADS:
            uploads.popitem(last=False)
        return {"token": token, "width": img.width, "height": img.height,
                "boxes": [{"index": i, "box": list(b[:4]), "conf": b[4]} for i, b in enumerate(boxes)]}

    def get_upload(token):
        up = uploads.get(token)
        if up is None:
            raise bad("upload expired or unknown; upload the photo again", 404)
        return up

    @app.get("/api/uploads/{token}/image")
    def upload_image(token: str):
        return Response(get_upload(token)["jpeg"], media_type="image/jpeg", headers=CACHE)

    def prepare(q: SearchQuery):
        with connect() as conn:
            profile_name(conn, q.profile_id)
            labels = conn.execute("select l.person_id, l.label, p.photo_id from labels l "
                                  "join persons p on p.id = l.person_id where l.profile_id = ?",
                                  (q.profile_id,)).fetchall()
        embedded = lambda ids: [p for p, ok in zip(ids, np.isin(ids, persons.ids)) if ok]
        me = [p for p, label, _ in labels if label == "me"]
        not_me = [p for p, label, _ in labels if label == "not_me"]
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
            up = get_upload(q.upload)
            row = len(up["boxes"]) if q.box == "whole" else q.box
            if q.box != "whole" and not 0 <= row < len(up["boxes"]):
                raise bad(f"box {q.box} out of range; valid boxes are 0..{len(up['boxes']) - 1} or whole"
                          if up["boxes"] else f"box {q.box}: no person detected in the upload; use whole")
            for k in ("osnet", "siglip"):
                v = up[k][row:row + 1]
                refs[k] = np.vstack([refs[k], v]) if k in refs else v
        negatives = search.person_refs(persons, not_me_emb)["osnet"] if not_me_emb else None
        exclude = {photo for _, label, photo in labels if label == "me"} if q.mode == "more" else set()
        drop = not_me if q.mode == "more" else []
        return refs, ref_ids, negatives, exclude | set(q.seen), drop, note

    def ranked(q, filters, top, offset, refs, ref_ids, negatives, exclude, drop, note):
        with connect() as conn:
            warnings = [note, search.check_filters(conn, filters)]
            if "scene" in refs:
                warnings.append(search.check_scenes(conn))
            mask = search.filter_mask(conn, persons, filters)
            t0 = time.monotonic()
            scores = search.score(persons, refs, negatives=negatives)
            keep = np.ones(len(scores), bool) if mask is None else mask
            if drop:
                keep &= ~np.isin(persons.ids, drop)
            rows = np.flatnonzero(keep)
            picked = [int(rows[i]) for i in
                      search.best_per_photo(persons.photo_ids[rows], scores[rows], offset + top, exclude)][offset:]
            timing = time.monotonic() - t0
            via = search.matched_via(persons, picked, refs, ref_ids)
            results = hydrate(conn, [(int(persons.ids[i]), float(scores[i])) for i in picked],
                              offset + len(set(q.seen)), q.profile_id, via)
        return {"results": results, "warnings": [w for w in warnings if w], "timing": round(timing, 4)}

    def bib_start(q, filters, top, offset):
        bib = q.start_bib.strip()
        if not bib:
            raise bad("start_bib needs a bib number, e.g. 8038")
        if q.persons or q.upload or (q.text or "").strip() or (q.scene or "").strip() or q.mode == "more":
            raise bad("start_bib cannot be combined with persons, upload, text, scene or find-more")
        with connect() as conn:
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

    @app.post("/api/search")
    async def search_photos(q: SearchQuery):
        filters = filters_of(q)
        top, offset = min(max(q.top, 1), MAX_TOP), max(q.offset, 0)
        if q.start_bib is not None:
            return await run_in_threadpool(bib_start, q, filters, top, offset)
        texts = {k: v.strip() for k, v in (("text", q.text), ("scene", q.scene)) if v and v.strip()}
        if "scene" in texts and scene_error:
            raise bad(scene_error)
        refs, ref_ids, negatives, exclude, drop, note = await run_in_threadpool(prepare, q)
        if not refs and not texts:
            raise bad("give a person, an uploaded box, text, scene or start_bib to search")
        if texts:
            vecs = await on_models(models.encode_text, list(texts.values()))
            refs.update({k: v[None] for k, v in zip(texts, vecs)})
        return await run_in_threadpool(ranked, q, filters, top, offset, refs, ref_ids, negatives, exclude, drop, note)

    @app.post("/api/labels")
    def set_label(body: LabelBody):
        with connect() as conn, conn:
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
                             "do update set label = excluded.label, created_at = excluded.created_at",
                             (*key, body.label, now()))
        return {"profile_id": body.profile_id, "person_id": body.person_id, "label": body.label,
                "previous": previous and previous[0]}

    @app.get("/api/photos/{photo_id}")
    def photo(photo_id: Id, profile_id: Id):
        with connect() as conn:
            profile_name(conn, profile_id)
            meta = photo_meta(conn, [photo_id]).get(photo_id)
            if meta is None:
                raise bad(f"photo {photo_id} not found", 404)
            rows = conn.execute("select id, x1, y1, x2, y2 from persons where photo_id = ? order by id",
                                (photo_id,)).fetchall()
            ids = [r[0] for r in rows]
            bibs, labels = bibs_of(conn, ids), labels_of(conn, ids, profile_id)
        return {**meta, "persons": [{"person_id": r[0], "box": list(r[1:]), "bibs": bibs.get(r[0], []),
                                     "label": labels.get(r[0])} for r in rows]}

    def photo_path(photo_id) -> Path:
        with connect() as conn:
            row = conn.execute("select relpath from photos where id = ?", (photo_id,)).fetchone()
        if row is None:
            raise bad(f"photo {photo_id} not found", 404)
        return collection / row[0]

    def open_photo(photo_id, path):
        try:
            return models.load_image(path)
        except Exception:
            raise bad(f"photo {photo_id} file is missing or unreadable", 404)

    @app.get("/api/photos/{photo_id}/image")
    def photo_image(photo_id: Id, size: int | None = Query(None, alias="max", ge=16, le=8192)):
        path = photo_path(photo_id)
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

    @app.get("/api/persons/{person_id}/crop")
    def person_crop(person_id: Id):
        with connect() as conn:
            row = conn.execute("select ph.id, ph.relpath, p.x1, p.y1, p.x2, p.y2 from persons p "
                               "join photos ph on ph.id = p.photo_id where p.id = ?", (person_id,)).fetchone()
        if row is None:
            raise bad(f"person {person_id} not found", 404)
        photo_id, relpath, x1, y1, x2, y2 = row
        img = open_photo(photo_id, collection / relpath)
        pw, ph = (x2 - x1) * CROP_PAD, (y2 - y1) * CROP_PAD
        img = models.crop(img, (x1 - pw, y1 - ph, x2 + pw, y2 + ph))
        if img.height > CROP_HEIGHT:
            img = img.resize((max(1, round(img.width * CROP_HEIGHT / img.height)), CROP_HEIGHT))
        return Response(jpeg(img), media_type="image/jpeg", headers=CACHE)

    def me_rows(conn, profile_id):
        return conn.execute("select ph.id, p.id, p.x1, p.y1, p.x2, p.y2 from labels l "
                            "join persons p on p.id = l.person_id join photos ph on ph.id = p.photo_id "
                            f"where l.profile_id = ? and l.label = 'me' {FIRST}, p.id", (profile_id,)).fetchall()

    def marked(conn, profile_id):
        ids = list(dict.fromkeys(r[0] for r in me_rows(conn, profile_id)))
        meta = photo_meta(conn, ids)
        return originals.rows_of(collection, [meta[i] for i in ids])

    @app.get("/api/me")
    def my_photos(profile_id: Id):
        with connect() as conn:
            name = profile_name(conn, profile_id)
            rows = me_rows(conn, profile_id)
            meta = photo_meta(conn, {r[0] for r in rows})
            bibs = bibs_of(conn, [r[1] for r in rows])
        status = originals.statuses(originals.profile_folder(collection, name),
                                    originals.rows_of(collection, list(meta.values())))
        photos = {}
        for photo_id, pid, *box in rows:
            photos.setdefault(photo_id, {**meta[photo_id], "original": status[photo_id], "persons": []})["persons"].append(
                {"person_id": pid, "box": box, "bibs": bibs.get(pid, []), "label": "me"})
        return {"photos": list(photos.values()), "count": len(photos)}

    @app.post("/api/export")
    def export(body: ExportBody):
        with connect() as conn:
            name = profile_name(conn, body.profile_id)
            rows = marked(conn, body.profile_id)
        if not rows:
            raise bad("no photos marked as me yet; nothing to export")
        return {"path": str(originals.write_csv(originals.profile_folder(collection, name), rows)), "count": len(rows)}

    def busy():
        if job.status()["state"] == "running":
            return bad("an originals download is already running", 409)
        return bad("a download is in progress; try again in a moment", 409)

    def need_yipai():
        if not yipai:
            raise bad("originals are only available for yipai360 collections (no manifest.sqlite)")

    @app.post("/api/originals")
    def start_originals(body: ExportBody):
        need_yipai()
        try:
            job.claim()
        except originals.Busy:
            raise busy()
        try:
            with connect() as conn:
                name = profile_name(conn, body.profile_id)
                rows = marked(conn, body.profile_id)
            if not rows:
                raise bad("no photos marked as me yet; nothing to download")
            job.start(body.profile_id, name, originals.profile_folder(collection, name), rows)
        except BaseException:
            job.busy.release()
            raise
        return job.status()

    @app.get("/api/originals")
    def originals_status():
        return job.status()

    @app.post("/api/originals/cancel")
    def cancel_originals():
        job.cancel()
        return job.status()

    @app.get("/api/originals/zip")
    def originals_zip(profile_id: Id):
        need_yipai()
        with connect() as conn:
            name = profile_name(conn, profile_id)
        folder = originals.profile_folder(collection, name)
        path = originals.zip_folder(folder)
        if path is None:
            raise bad(f"no originals downloaded yet for {name}", 404)
        return FileResponse(path, media_type="application/zip", filename=f"{collection.name}-{folder.name}-originals.zip",
                            background=BackgroundTask(path.unlink, missing_ok=True))

    @app.post("/api/photos/{photo_id}/original")
    def photo_original(photo_id: Id, body: ExportBody):
        need_yipai()
        try:
            with job.claimed():
                with connect() as conn:
                    name = profile_name(conn, body.profile_id)
                    meta = photo_meta(conn, [photo_id]).get(photo_id)
                    if meta is None:
                        raise bad(f"photo {photo_id} not found", 404)
                    rows = marked(conn, body.profile_id)
                row = originals.rows_of(collection, [meta])[0]
                result, dest = job.single(row, originals.profile_folder(collection, name), rows)
        except originals.Busy:
            raise busy()
        except originals.Cancelled:
            raise bad("download was cancelled; try again", 409)
        except Blocked as e:
            raise bad(f"yipai360 API unavailable: {e}", 502)
        if result.startswith("buy on site"):
            raise bad(result, 402)
        if result != originals.DOWNLOADED:
            raise bad(result, 502)
        return FileResponse(dest, media_type="image/jpeg", filename=dest.name)

    return app
