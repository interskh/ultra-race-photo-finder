import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import ImageOps

from photofinder import models, search

CONFIGS = {"osnet": {"osnet": 1.0}, "siglip": {"siglip": 1.0}, "0.3:0.7": {"osnet": 0.3, "siglip": 0.7},
           "0.5:0.5": {"osnet": 0.5, "siglip": 0.5}, "0.7:0.3": {"osnet": 0.7, "siglip": 0.3}}
KS = (10, 50)
SHEET_TOP = 30
MIN_PHOTOS = 2


@dataclass
class Truth:
    bib: str
    photos: dict
    refs: list
    persons: set = field(default_factory=set)


@dataclass
class Row:
    config: str
    r10: float
    r50: float
    x50: float
    s10: float
    s50: float
    refs: int
    xrefs: int
    photos: int


def configs(weights=search.WEIGHTS) -> dict:
    current = {k: weights[k] for k in ("osnet", "siglip")}
    out = dict(CONFIGS)
    if not any(_same(current, c) for c in out.values()):
        out[f"default {current['osnet']}:{current['siglip']}"] = current
    return out


def _same(a, b):
    return all(abs(a.get(k, 0) / sum(a.values()) - b.get(k, 0) / sum(b.values())) < 1e-9 for k in ("osnet", "siglip"))


def ground_truth(db: sqlite3.Connection, bib: str, persons: search.Persons, cap: int = 20) -> Truth:
    rows = db.execute("""select p.photo_id, p.id, coalesce(ph.photographer_uid, ph.photographer)
                         from bibs b join persons p on p.id = b.person_id join photos ph on ph.id = p.photo_id
                         where ph.status = 'ok' and b.text = ?
                         order by p.photo_id, (p.x2 - p.x1) * (p.y2 - p.y1) desc, p.id""", (bib,)).fetchall()
    embedded = set(persons.ids.tolist())
    photos, refs = {}, {}
    for photo, pid, who in rows:
        photos[photo] = who
        if pid in embedded:
            refs.setdefault(photo, pid)
    return Truth(bib, photos, [(pid, photo) for photo, pid in refs.items()][:cap], {pid for _, pid, _ in rows})


def recall(ranked, truth: set, k: int) -> float | None:
    return len(set(ranked[:k]) & truth) / len(truth) if truth else None


def rank_photos(persons: search.Persons, ref: int, ref_photo: int, weights, top=None):
    scores = search.score(persons, search.person_refs(persons, [ref]), weights)
    picked = search.best_per_photo(persons.photo_ids, scores, top or KS[1], exclude=[ref_photo])
    return picked, scores


def evaluate_ref(persons: search.Persons, truth: Truth, ref: int, ref_photo: int, weights):
    gt = set(truth.photos) - {ref_photo}
    if not gt:
        return None
    picked, _ = rank_photos(persons, ref, ref_photo, weights)
    ranked = [int(persons.photo_ids[i]) for i in picked]
    strict = [p if int(persons.ids[i]) in truth.persons else None for p, i in zip(ranked, picked)]
    who = truth.photos[ref_photo]
    cross = {p for p in gt if who is not None and truth.photos[p] is not None and truth.photos[p] != who}
    return (recall(ranked, gt, KS[0]), recall(ranked, gt, KS[1]), recall(ranked, cross, KS[1]),
            recall(strict, gt, KS[0]), recall(strict, gt, KS[1]))


def evaluate_bib(persons: search.Persons, truth: Truth, weights_by_name: dict) -> list[Row]:
    rows = []
    for name, weights in weights_by_name.items():
        got = [r for pid, photo in truth.refs if (r := evaluate_ref(persons, truth, pid, photo, weights))]
        xs = [r[2] for r in got if r[2] is not None]
        rows.append(Row(name, _mean([r[0] for r in got]), _mean([r[1] for r in got]), _mean(xs),
                        _mean([r[3] for r in got]), _mean([r[4] for r in got]), len(got), len(xs), len(truth.photos)))
    return rows


def _mean(xs) -> float:
    return float(np.mean(xs)) if xs else float("nan")


def mean_over_bibs(per_bib: list[list[Row]]) -> list[Row]:
    out = []
    for rows in zip(*per_bib):
        means = (float(np.nanmean([getattr(r, a) for r in rows])) for a in ("r10", "r50", "x50", "s10", "s50"))
        out.append(Row(rows[0].config, *means, sum(r.refs for r in rows), sum(r.xrefs for r in rows),
                       sum(r.photos for r in rows)))
    return out


def frequent_bibs(db: sqlite3.Connection, limit: int = 20) -> list[tuple[str, int, int]]:
    return db.execute("""select b.text, count(distinct p.photo_id),
                                count(distinct coalesce(ph.photographer_uid, ph.photographer))
                         from bibs b join persons p on p.id = b.person_id join photos ph on ph.id = p.photo_id
                         where ph.status = 'ok' group by b.text
                         order by length(b.text) = 4 desc, 2 desc, b.text limit ?""", (limit,)).fetchall()


def sheet(db: sqlite3.Connection, collection: Path, persons: search.Persons, truth: Truth, weights, out: Path):
    ref, ref_photo = truth.refs[0]
    picked, scores = rank_photos(persons, ref, ref_photo, weights, SHEET_TOP)
    gt = set(truth.photos) - {ref_photo}
    ids = [ref] + [int(persons.ids[i]) for i in picked]
    marks = ",".join("?" * len(ids))
    bibs = {}
    for pid, text in db.execute(f"select person_id, text from bibs where person_id in ({marks}) order by conf desc",
                                ids):
        bibs.setdefault(pid, []).append(text)
    tiles = [(_crop(db, collection, persons, ref), f"query\nbib {truth.bib}\n" + _photo_id(db, ref_photo))]
    for rank, i in enumerate(picked, 1):
        photo, pid = int(persons.photo_ids[i]), int(persons.ids[i])
        img = _crop(db, collection, persons, pid)
        mark = ("BIB" if pid in truth.persons else "bib-photo") if photo in gt else ""
        if mark:
            img = ImageOps.expand(img, max(4, img.height // 40), fill=(0, 200, 0) if mark == "BIB" else (255, 150, 0))
        label = f"#{rank} {scores[i]:.3f} {mark}".rstrip() + f"\n{_photo_id(db, photo)}"
        if pid in bibs:
            label += "\nocr " + "/".join(bibs[pid])
        tiles.append((img, label))
    search.contact_sheet(tiles, out, label=40)


def _photo_id(db, photo) -> str:
    src, = db.execute("select coalesce(source_photo_id, id) from photos where id = ?", (photo,)).fetchone()
    return f"id {src}"


def _crop(db, collection: Path, persons: search.Persons, pid: int):
    i = int(np.flatnonzero(persons.ids == pid)[0])
    relpath, = db.execute("select relpath from photos where id = ?", (int(persons.photo_ids[i]),)).fetchone()
    return models.crop(models.load_image(collection / relpath), persons.boxes[i])
