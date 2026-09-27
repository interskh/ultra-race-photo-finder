import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from photofinder import models

TABLES = {"osnet": "emb_person_osnet", "siglip": "emb_person_siglip"}
WEIGHTS = {"osnet": 0.3, "siglip": 0.7, "text": 0.5, "scene": 0.5}
SOURCES = {"text": "siglip"}
NEG_WEIGHT = 0.3
CHUNK = 8192
TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")

log = logging.getLogger("search")


class MissingEmbeddings(Exception):
    pass


@dataclass
class Persons:
    ids: np.ndarray
    photo_ids: np.ndarray
    boxes: np.ndarray
    vecs: dict[str, np.ndarray]
    scene: tuple[np.ndarray, np.ndarray] | None = None


@dataclass
class Result:
    rank: int
    score: float
    person_id: int
    photo_id: int
    box: tuple
    relpath: str
    source_photo_id: str | None
    taken_at: str | None
    photographer: str | None
    album: str | None


@dataclass
class Filters:
    start: str | None = None
    end: str | None = None
    photographers: tuple = ()
    albums: tuple = ()
    bib: str | None = None

    def __bool__(self):
        return any((self.start, self.end, self.photographers, self.albums, self.bib))


def parse_time(value: str | None, minute_end=False) -> str | None:
    if value is None:
        return None
    for fmt in TIME_FORMATS:
        try:
            t = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return t.replace(second=59).strftime(TIME_FORMATS[0]) if minute_end and fmt == TIME_FORMATS[1] \
            else t.strftime(TIME_FORMATS[0])
    raise ValueError(f"{value!r} is not a time; use 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD HH:MM:SS'")


def check_filters(db: sqlite3.Connection, filters: Filters | None) -> str | None:
    if not (filters and filters.bib):
        return None
    total, unread = db.execute("""select count(*), count(*) - count(p.ocr_at) from persons p
                                  join photos ph on ph.id = p.photo_id where ph.status = 'ok'""").fetchone()
    if total == unread:
        raise MissingEmbeddings("index has no bib OCR yet (stage ocr_bibs); run `photofinder index <collection>` first")
    if unread:
        return f"ocr_bibs incomplete: {unread} of {total} persons not read yet; rerun `photofinder index`"
    return None


def filter_where(filters: Filters) -> tuple[list[str], list]:
    where, args = ["ph.status = 'ok'"], []
    if filters.start:
        where.append("ph.taken_at >= ?")
        args.append(filters.start)
    if filters.end:
        where.append("ph.taken_at <= ?")
        args.append(filters.end)
    if filters.photographers:
        marks = ",".join("?" * len(filters.photographers))
        where.append(f"(ph.photographer in ({marks}) or ph.photographer_uid in ({marks}))")
        args += [*filters.photographers, *filters.photographers]
    if filters.albums:
        where.append(f"ph.album in ({','.join('?' * len(filters.albums))})")
        args += filters.albums
    if filters.bib:
        where.append("exists (select 1 from bibs b where b.person_id = p.id and instr(b.text, ?) > 0)")
        args.append(filters.bib)
    return where, args


def filter_mask(db: sqlite3.Connection, persons: Persons, filters: Filters | None) -> np.ndarray | None:
    if not filters:
        return None
    check_filters(db, filters)
    where, args = filter_where(filters)
    ids = [i for (i,) in db.execute("select p.id from persons p join photos ph on ph.id = p.photo_id where "
                                    + " and ".join(where), args)]
    return np.isin(persons.ids, ids)


def matrix(blobs) -> np.ndarray:
    return models.l2norm(np.frombuffer(b"".join(blobs), dtype=np.float16).reshape(len(blobs), -1)).astype(np.float16)


def load_persons(db: sqlite3.Connection) -> Persons:
    t0 = time.monotonic()
    rows = db.execute("""select p.id, p.photo_id, p.x1, p.y1, p.x2, p.y2, o.v, s.v from persons p
                         join photos ph on ph.id = p.photo_id
                         join emb_person_osnet o on o.person_id = p.id
                         join emb_person_siglip s on s.person_id = p.id
                         where ph.status = 'ok' order by p.id""").fetchall()
    if not rows:
        raise MissingEmbeddings("index has no person embeddings (stage embed_persons); "
                                "run `photofinder index <collection>` first")
    ids, photo_ids, x1, y1, x2, y2, osnet, siglip = zip(*rows)
    persons = Persons(np.array(ids), np.array(photo_ids), np.array([x1, y1, x2, y2], dtype=np.float32).T,
                      {"osnet": matrix(osnet), "siglip": matrix(siglip)})
    log.info("loaded %d person embeddings in %.3fs", len(rows), time.monotonic() - t0)
    return persons


def load_scenes(db: sqlite3.Connection, persons: Persons) -> Persons:
    t0 = time.monotonic()
    rows = db.execute("""select e.photo_id, e.v from emb_scene_siglip e join photos ph on ph.id = e.photo_id
                         where ph.status = 'ok'""").fetchall()
    if not rows:
        raise MissingEmbeddings("index has no scene embeddings (stage embed_scenes); "
                                "run `photofinder index <collection>` first")
    photo_ids, blobs = zip(*rows)
    vecs = matrix(blobs)
    vecs = np.vstack([vecs, np.zeros((1, vecs.shape[1]), vecs.dtype)])
    row = {p: i for i, p in enumerate(photo_ids)}
    persons.scene = vecs, np.array([row.get(p, len(rows)) for p in persons.photo_ids], dtype=np.int64)
    log.info("loaded %d scene embeddings in %.3fs", len(rows), time.monotonic() - t0)
    return persons


def check_scenes(db: sqlite3.Connection) -> str | None:
    total, missing = db.execute("""select count(*), count(*) - count(e.photo_id) from photos ph
                                   left join emb_scene_siglip e on e.photo_id = ph.id
                                   where ph.status = 'ok'""").fetchone()
    if missing:
        return (f"embed_scenes incomplete: {missing} of {total} photos have no scene vector yet; "
                "rerun `photofinder index`")
    return None


def person_refs(persons: Persons, person_ids) -> dict[str, np.ndarray]:
    rows = np.flatnonzero(np.isin(persons.ids, person_ids))
    return {k: v[rows] for k, v in persons.vecs.items()}


def max_cos(vecs: np.ndarray, refs) -> np.ndarray:
    refs = models.l2norm(refs).T
    out = np.empty(len(vecs), dtype=np.float32)
    for i in range(0, len(vecs), CHUNK):
        out[i:i + CHUNK] = (models.l2norm(vecs[i:i + CHUNK]) @ refs).max(axis=1)
    return out


def term_scores(persons: Persons, key: str, refs) -> np.ndarray:
    if key == "scene":
        vecs, rows = persons.scene
        out = max_cos(vecs, refs)[rows]
        out[rows == len(vecs) - 1] = np.nan
        return out
    return max_cos(persons.vecs[SOURCES.get(key, key)], refs)


def rank_invalid_last(s: np.ndarray) -> np.ndarray:
    ok = np.isfinite(s)
    if ok.all():
        return s
    out = s.copy()
    out[~ok] = s[ok].min() - 1 if ok.any() else -1.0
    return out


def zscore(s: np.ndarray) -> np.ndarray:
    ok = np.isfinite(s)
    out = np.full_like(s, np.nan)
    std = s[ok].std() if ok.any() else 0
    out[ok] = (s[ok] - s[ok].mean()) / std if std > 0 else 0
    return rank_invalid_last(out)


def score(persons: Persons, refs: dict, weights=WEIGHTS, negatives=None) -> np.ndarray:
    terms = [(weights[k], term_scores(persons, k, r)) for k, r in refs.items()
             if r is not None and len(r) and weights.get(k)]
    if not terms:
        raise ValueError("no query terms to score")
    norm = zscore if len(terms) > 1 else rank_invalid_last
    total = sum(w for w, _ in terms)
    out = sum(w * norm(s) for w, s in terms) / total
    if negatives is not None and len(negatives):
        out = out - NEG_WEIGHT / total * norm(max_cos(persons.vecs["osnet"], negatives))
    return out


def best_per_photo(photo_ids: np.ndarray, scores: np.ndarray, top: int, exclude=()) -> list[int]:
    picked, seen = [], set(exclude)
    for i in np.argsort(-scores, kind="stable"):
        if photo_ids[i] not in seen:
            seen.add(photo_ids[i])
            picked.append(int(i))
            if len(picked) == top:
                break
    return picked


def search(db: sqlite3.Connection, refs: dict, top: int = 24, exclude=(), weights=WEIGHTS,
           persons: Persons | None = None, filters: Filters | None = None) -> list[Result]:
    persons = persons or load_persons(db)
    if refs.get("scene") is not None and persons.scene is None:
        load_scenes(db, persons)
    mask = filter_mask(db, persons, filters)
    t0 = time.monotonic()
    scores = score(persons, refs, weights)
    rows = np.arange(len(scores)) if mask is None else np.flatnonzero(mask)
    picked = [int(rows[i]) for i in best_per_photo(persons.photo_ids[rows], scores[rows], top, exclude)]
    log.info("scored %d persons in %.3fs", len(scores), time.monotonic() - t0)
    results = []
    for rank, i in enumerate(picked, 1):
        photo_id = int(persons.photo_ids[i])
        meta = db.execute("select relpath, source_photo_id, taken_at, photographer, album from photos "
                          "where id = ?", (photo_id,)).fetchone()
        results.append(Result(rank, float(scores[i]), int(persons.ids[i]), photo_id,
                              tuple(float(x) for x in persons.boxes[i]), *meta))
    return results


def contact_sheet(tiles: list[tuple[Image.Image, str]], out: Path, height=256, width=1800, label=22):
    tiles = [(img.resize((max(1, round(img.width * height / img.height)), height)), text) for img, text in tiles]
    tiles = [(img.resize((width, max(1, round(img.height * width / img.width)))) if img.width > width else img, text)
             for img, text in tiles]
    rows, row, x = [], [], 0
    for img, text in tiles:
        if row and x + img.width > width:
            rows.append(row)
            row, x = [], 0
        row.append((img, text))
        x += img.width + 4
    rows.append(row)
    sheet = Image.new("RGB", (width, len(rows) * (height + label + 4)), "white")
    draw = ImageDraw.Draw(sheet)
    for r, row in enumerate(rows):
        x, y = 0, r * (height + label + 4)
        for img, text in row:
            sheet.paste(img, (x, y))
            draw.text((x + 2, y + height + 3), text, fill="black")
            x += img.width + 4
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=88)


def find_photo(db: sqlite3.Connection, collection: Path, path: Path) -> tuple[int, str] | None:
    names = {path.name.casefold(), path.resolve().name.casefold()}
    for photo_id, relpath in db.execute("select id, relpath from photos"):
        if relpath.rsplit("/", 1)[-1].casefold() in names and same_file(collection / relpath, path):
            return photo_id, relpath
    return None


def same_file(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False
