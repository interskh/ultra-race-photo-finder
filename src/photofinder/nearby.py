import dataclasses
import sqlite3

import numpy as np

from photofinder import models, search

ORDER = "photographer_uid, taken_ts, length(source_photo_id), source_photo_id, id"
SPAN_MAX = 5
NOT_INDEXED = "this photo is not part of the index"
NO_PHOTOGRAPHER = "this photo has no photographer, so it has no roll of shots"
NO_TIME = "this photo has no capture time, so its place in the photographer's roll is unknown"


def marks(values) -> str:
    return ",".join("?" * len(values))


def rolls(conn: sqlite3.Connection, anchors, span: int) -> tuple[dict, dict]:
    anchors = list(dict.fromkeys(anchors))
    info = conn.execute(f"select id, photographer_uid, taken_ts, status from photos where id in ({marks(anchors)})",
                        anchors).fetchall()
    reasons, uids = {}, set()
    for photo, uid, ts, status in info:
        if status != "ok":
            reasons[photo] = NOT_INDEXED
        elif uid is None:
            reasons[photo] = NO_PHOTOGRAPHER
        elif ts is None:
            reasons[photo] = NO_TIME
        else:
            uids.add(uid)
    seqs = {}
    for photo, uid, ts in conn.execute("select id, photographer_uid, taken_ts from photos where status = 'ok' "
                                       f"and taken_ts is not null and photographer_uid in ({marks(uids)}) "
                                       f"order by {ORDER}", list(uids)):
        seqs.setdefault(uid, []).append((photo, ts))
    wanted, out = {p for p, *_ in info if p not in reasons}, {}
    for seq in seqs.values():
        pos = {p: i for i, (p, _) in enumerate(seq)}
        for a in pos.keys() & wanted:
            i, ts = pos[a], seq[pos[a]][1]
            out[a] = [(p, j - i, t - ts, t) for j in range(max(0, i - span), min(len(seq), i + span + 1))
                      if j != i for p, t in [seq[j]]]
    return out, reasons


def rows_of(persons: search.Persons, person_ids) -> list[int]:
    ids = np.asarray(list(person_ids), dtype=persons.ids.dtype)
    rows = np.searchsorted(persons.ids, ids)
    rows = np.minimum(rows, len(persons.ids) - 1)
    return [int(r) for r in rows[persons.ids[rows] == ids]]


def rows_by_photo(persons: search.Persons, photo_ids) -> dict[int, list[int]]:
    out = {}
    for r in np.flatnonzero(np.isin(persons.photo_ids, list(photo_ids))):
        out.setdefault(int(persons.photo_ids[r]), []).append(int(r))
    return out


def best_match(persons: search.Persons, rows, refs) -> tuple[int, int, float] | None:
    if not rows or not refs:
        return None
    sim = sum(search.WEIGHTS[k] * np.nan_to_num(models.l2norm(persons.vecs[k][rows])
                                               @ models.l2norm(persons.vecs[k][refs]).T)
              for k in ("osnet", "siglip"))
    i, j = np.unravel_index(int(np.argmax(sim)), sim.shape)
    return int(persons.ids[rows[i]]), int(persons.ids[refs[j]]), float(sim[i, j])


def shot(photo, offset, gap, **extra) -> dict:
    return {"photo_id": photo, "offset": offset, "gap_s": gap, "same_second": gap == 0, **extra}


def collect(conn: sqlite3.Connection, persons: search.Persons, profile_id: int, span: int,
            filters: search.Filters, bib: str | None = None) -> tuple[list[dict], list[str]]:
    labels = conn.execute("select l.person_id, l.label, p.photo_id from labels l join persons p on p.id = l.person_id "
                          "where l.profile_id = ?", (profile_id,)).fetchall()
    not_me = {p for p, label, _ in labels if label == "not_me"}
    not_me_photos = {photo for _, label, photo in labels if label == "not_me"}
    refs = {}
    for p, label, photo in labels:
        if label == "me":
            refs.setdefault(photo, []).append(p)
    hidden = set()
    if bib:
        for p, photo in conn.execute("select p.id, p.photo_id from bibs b join persons p on p.id = b.person_id "
                                     "where b.text = ? order by p.id", (bib,)):
            hidden.add(photo)
            if p not in not_me:
                refs.setdefault(photo, []).append(p)
    anchors = {a: rows_of(persons, dict.fromkeys(ps)) for a, ps in refs.items()}
    blind = sorted(a for a, rows in anchors.items() if not rows)
    neighbours, _ = rolls(conn, [a for a in anchors if anchors[a]], span)
    near = {}
    for a, shots in neighbours.items():
        for photo, offset, gap, ts in shots:
            key = (abs(offset), abs(gap), a)
            if photo not in anchors and photo not in hidden and (photo not in near or key < near[photo][0]):
                near[photo] = (key, a, offset, gap, ts)
    ids = list(near)
    where, args = search.filter_where(dataclasses.replace(filters, bib=None))
    ok = {p for p, in conn.execute(f"select ph.id from photos ph where {' and '.join(where)} "
                                   f"and ph.id in ({marks(ids)})", [*args, *ids])}
    allowed = None
    if filters.bib:
        where, args = search.filter_where(filters)
        allowed = {p for p, in conn.execute("select p.id from persons p join photos ph on ph.id = p.photo_id where "
                                            f"{' and '.join(where)} and ph.id in ({marks(ids)})", [*args, *ids])}
    by_photo = rows_by_photo(persons, ok)
    out = []
    for photo in ok:
        _, a, offset, gap, ts = near[photo]
        rows = [r for r in by_photo.get(photo, []) if int(persons.ids[r]) not in not_me
                and (allowed is None or int(persons.ids[r]) in allowed)]
        if not rows and (allowed is not None or photo in not_me_photos):
            continue
        match = best_match(persons, rows, anchors[a])
        pid, ref, sim = match or (None, int(persons.ids[anchors[a][0]]), None)
        out.append(shot(photo, offset, gap, person_id=pid, similarity=sim, anchor_photo_id=a,
                        anchor_person_id=ref, taken_ts=ts))
    out.sort(key=lambda s: (abs(s["offset"]), s["taken_ts"], s["photo_id"]))
    warnings = [f"{len(blind)} confirmed photo{'s have' if len(blind) > 1 else ' has'} no embedded reference person "
                "yet; nearby shots skip it until you rerun `photofinder index`"] if blind else []
    return out, warnings
