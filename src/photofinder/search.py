import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from photofinder import models

TABLES = {"osnet": "emb_person_osnet", "siglip": "emb_person_siglip"}
WEIGHTS = {"osnet": 0.5, "siglip": 0.5}
CHUNK = 65536

log = logging.getLogger("search")


class MissingEmbeddings(Exception):
    pass


@dataclass
class Persons:
    ids: np.ndarray
    photo_ids: np.ndarray
    boxes: np.ndarray
    vecs: dict[str, np.ndarray]


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


def matrix(blobs) -> np.ndarray:
    return models.l2norm(np.frombuffer(b"".join(blobs), dtype=np.float16).reshape(len(blobs), -1))


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


def person_refs(persons: Persons, person_ids) -> dict[str, np.ndarray]:
    rows = np.flatnonzero(np.isin(persons.ids, person_ids))
    return {k: v[rows] for k, v in persons.vecs.items()}


def max_cos(vecs: np.ndarray, refs) -> np.ndarray:
    refs = models.l2norm(refs).T
    out = np.empty(len(vecs), dtype=np.float32)
    for i in range(0, len(vecs), CHUNK):
        out[i:i + CHUNK] = (vecs[i:i + CHUNK] @ refs).max(axis=1)
    return out


def score(persons: Persons, refs: dict, weights=WEIGHTS) -> np.ndarray:
    terms = [(weights[k], max_cos(persons.vecs[k], r)) for k, r in refs.items()
             if r is not None and len(r) and weights.get(k)]
    if not terms:
        raise ValueError("no query terms to score")
    return sum(w * s for w, s in terms) / sum(w for w, _ in terms)


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
           persons: Persons | None = None) -> list[Result]:
    persons = persons or load_persons(db)
    t0 = time.monotonic()
    scores = score(persons, refs, weights)
    picked = best_per_photo(persons.photo_ids, scores, top, exclude)
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
    target = path.resolve()
    names = {path.name, target.name}
    for photo_id, relpath in db.execute("select id, relpath from photos"):
        if relpath.rsplit("/", 1)[-1] in names and (collection / relpath).resolve() == target:
            return photo_id, relpath
    return None
