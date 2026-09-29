import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from photofinder import models, races
from photofinder.memory import AdaptiveBatcher
from photofinder.sources import yipai

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
MANIFEST_NAME = "manifest.sqlite"
ALBUMS = "albums"
EXIF_IFD, DATETIME_ORIGINAL, MODEL, ORIENTATION = 0x8769, 0x9003, 0x0110, 0x0112
COMMIT_EVERY = 200
DETECT_BATCH, EMBED_BATCH, SCENE_BATCH, OCR_BATCH = 8, 64, 16, 64
OCR_MIN_HEIGHT, OCR_UPSCALE_BELOW = 200, 700
BIB_TOKEN = re.compile(r"(?<!\w)[0-9]{3,5}(?!\w)")

log = logging.getLogger("index")


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def find_images(collection: Path) -> list[str]:
    found = []
    for root, _, files in os.walk(collection):
        for name in files:
            path = Path(root) / name
            if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
                found.append(path.relative_to(collection).as_posix())
    return sorted(found)


def load_catalog(folder: Path) -> dict[str, tuple]:
    path = folder / MANIFEST_NAME
    if not path.is_file():
        return {}
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        has_view = db.execute("select 1 from sqlite_master where name = 'catalog'").fetchone()
        rows = db.execute("select source_id, photographer_uid, photographer, group_name, taken_at from "
                          + ("catalog" if has_view else f"({yipai.CATALOG_SELECT})")).fetchall()
    return {sid: rest for sid, *rest in rows}


def shot_time(taken: datetime) -> tuple[str, float]:
    return taken.strftime("%Y-%m-%d %H:%M:%S"), taken.timestamp()


def catalog_time(value: str | None) -> tuple:
    try:
        return shot_time(datetime.strptime(value, "%Y-%m-%d %H:%M:%S")) if value else (None, None)
    except ValueError:
        return None, None


def read_exif(img: Image.Image) -> tuple:
    try:
        exif = img.getexif()
        camera = exif.get(MODEL)
        camera = str(camera).strip("\x00 ") or None if camera else None
        raw = exif.get_ifd(EXIF_IFD).get(DATETIME_ORIGINAL)
        taken = datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S") if raw else None
    except Exception:
        return None, None, None
    if taken is None:
        return None, None, camera
    return *shot_time(taken), camera


def album_of(relpath: str) -> str | None:
    parts = relpath.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == ALBUMS else None


def load_albums(collection: Path, files: list[str]) -> dict[str, tuple]:
    registered = {a.key: a for r in races.load().races for a in r.albums}
    out = {}
    for key in sorted({k for k in map(album_of, files) if k}):
        album = registered.get(key)
        platform = album.platform if album else key.split("-", 1)[0]
        out[key] = (album.title if album and album.title else key, platform,
                    load_catalog(collection / ALBUMS / key))
    return out


def scan(db: sqlite3.Connection, collection: Path) -> dict:
    files = find_images(collection)
    is_race = (collection / ALBUMS).is_dir()
    if is_race:
        albums = load_albums(collection, files)
        for entry in sorted((collection / ALBUMS).iterdir()):
            if entry.is_symlink() and entry.is_dir():
                log.warning("album %s is a symlinked directory and will not be scanned; "
                            "use a real directory (file symlinks inside it are fine)", entry.name)
        stray = [f for f in files if album_of(f) is None]
        if stray:
            log.warning("skipping %d images not under %s/<album>/ in a race directory, e.g. %s",
                        len(stray), ALBUMS, stray[0])
    else:
        catalog = load_catalog(collection)
        stray = []
    existing = {r for (r,) in db.execute("select relpath from photos")}
    counts = {"new": 0, "existing": 0, "errors": 0, "skipped": len(stray)}
    for relpath in files:
        if relpath in existing:
            counts["existing"] += 1
            continue
        stem = Path(relpath).stem
        key = title = platform = None
        if is_race:
            key = album_of(relpath)
            if key is None:
                continue
            title, platform, catalog = albums[key]
        uid, photographer, grp, listed_at = catalog.get(stem, (None, None, None, None))
        if is_race and uid is not None:
            uid = f"{platform}:{uid}"
        width = height = taken_at = taken_ts = camera = error = None
        status = "ok"
        try:
            with Image.open(collection / relpath) as img:
                width, height = img.size
                if img.getexif().get(ORIENTATION) in (5, 6, 7, 8):
                    width, height = height, width
                taken_at, taken_ts, camera = read_exif(img)
        except Exception as e:
            status, error = "error", f"{type(e).__name__}: {e}"
            counts["errors"] += 1
            log.warning("unreadable image %s: %s", relpath, error)
        if taken_at is None:
            taken_at, taken_ts = catalog_time(listed_at)
        db.execute("""insert into photos(relpath, source_photo_id, width, height, taken_at, taken_ts, camera,
                      photographer_uid, photographer, album, album_key, grp, scanned_at, status, error)
                      values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (relpath, stem, width, height, taken_at, taken_ts, camera, uid, photographer, title, key, grp,
                    now(), status, error))
        counts["new"] += 1
        if counts["new"] % COMMIT_EVERY == 0:
            db.commit()
    db.commit()
    log.info("scan: %d new (%d errors), %d already indexed, %d skipped", counts["new"], counts["errors"],
             counts["existing"], counts["skipped"])
    return counts


def error_text(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def to_blob(v) -> bytes:
    return models.l2norm(v).astype(np.float16).tobytes()


def detect(db: sqlite3.Connection, collection: Path, detector=None, batcher=None) -> dict:
    pending = db.execute("select id, relpath from photos where status = 'ok' and detected_at is null "
                         "order by id").fetchall()
    counts = {"pending": len(pending), "photos": 0, "persons": 0, "errors": 0}
    log.info("detect: %d pending", len(pending))
    if not pending:
        return counts
    detector = detector or models.detect_persons
    batcher = batcher or AdaptiveBatcher(DETECT_BATCH)
    for chunk in batcher.chunks(pending):
        ids, images, errors = [], [], []
        for photo_id, relpath in chunk:
            try:
                images.append(models.load_image(collection / relpath))
                ids.append(photo_id)
            except Exception as e:
                errors.append((error_text(e), photo_id))
                log.warning("unreadable image %s: %s", relpath, errors[-1][0])
        found = detector(images) if images else []
        stamp = now()
        with db:
            db.executemany("update photos set status = 'error', error = ? where id = ?", errors)
            for photo_id, boxes in zip(ids, found, strict=True):
                db.executemany("insert into persons(photo_id, x1, y1, x2, y2, conf) values (?,?,?,?,?,?)",
                               [(photo_id, *box) for box in boxes])
                db.execute("update photos set detected_at = ? where id = ?", (stamp, photo_id))
        counts["persons"] += sum(map(len, found))
        counts["photos"] += len(ids)
        counts["errors"] += len(errors)
    log.info("detect: %d photos, %d persons, %d errors", counts["photos"], counts["persons"], counts["errors"])
    return counts


def embed_persons(db: sqlite3.Connection, collection: Path, embedder=None, batcher=None) -> dict:
    pending = db.execute("""select p.id, p.photo_id, ph.relpath, p.x1, p.y1, p.x2, p.y2 from persons p
                            join photos ph on ph.id = p.photo_id
                            where p.embedded_at is null and ph.status = 'ok'
                            order by p.photo_id, p.id""").fetchall()
    counts = {"pending": len(pending), "persons": 0, "errors": 0}
    log.info("embed_persons: %d pending", len(pending))
    if not pending:
        return counts
    embedder = embedder or models.embed_crops
    batcher = batcher or AdaptiveBatcher(EMBED_BATCH)
    for chunk in batcher.chunks(pending):
        images, bad = {}, {}
        for _, photo_id, relpath, *_ in chunk:
            if photo_id not in images and photo_id not in bad:
                try:
                    images[photo_id] = models.load_image(collection / relpath)
                except Exception as e:
                    bad[photo_id] = error_text(e)
                    log.warning("unreadable image %s: %s", relpath, bad[photo_id])
        todo = [(pid, models.crop(images[photo_id], box)) for pid, photo_id, _, *box in chunk
                if photo_id in images]
        reid, clip = embedder([c for _, c in todo]) if todo else ([], [])
        stamp = now()
        with db:
            db.executemany("update photos set status = 'error', error = ? where id = ?",
                           [(e, photo_id) for photo_id, e in bad.items()])
            for (pid, _), r, c in zip(todo, reid, clip, strict=True):
                db.execute("insert into emb_person_osnet(person_id, v) values (?,?)", (pid, to_blob(r)))
                db.execute("insert into emb_person_siglip(person_id, v) values (?,?)", (pid, to_blob(c)))
                db.execute("update persons set embedded_at = ? where id = ?", (stamp, pid))
        counts["persons"] += len(todo)
        counts["errors"] += len(bad)
    log.info("embed_persons: %d persons embedded, %d photo errors", counts["persons"], counts["errors"])
    return counts


def embed_scenes(db: sqlite3.Connection, collection: Path, embedder=None, batcher=None) -> dict:
    pending = db.execute("select id, relpath from photos where status = 'ok' and scene_done_at is null "
                         "order by id").fetchall()
    counts = {"pending": len(pending), "photos": 0, "errors": 0}
    log.info("embed_scenes: %d pending", len(pending))
    if not pending:
        return counts
    embedder = embedder or models.embed_images
    batcher = batcher or AdaptiveBatcher(SCENE_BATCH)
    for chunk in batcher.chunks(pending):
        ids, images, errors = [], [], []
        for photo_id, relpath in chunk:
            try:
                images.append(models.load_image(collection / relpath))
                ids.append(photo_id)
            except Exception as e:
                errors.append((error_text(e), photo_id))
                log.warning("unreadable image %s: %s", relpath, errors[-1][0])
        vecs = embedder(images) if images else []
        stamp = now()
        with db:
            db.executemany("update photos set status = 'error', error = ? where id = ?", errors)
            for photo_id, v in zip(ids, vecs, strict=True):
                db.execute("insert into emb_scene_siglip(photo_id, v) values (?,?)", (photo_id, to_blob(v)))
                db.execute("update photos set scene_done_at = ? where id = ?", (stamp, photo_id))
        counts["photos"] += len(ids)
        counts["errors"] += len(errors)
    log.info("embed_scenes: %d photos embedded, %d errors", counts["photos"], counts["errors"])
    return counts


def bib_tokens(found) -> dict[str, float]:
    tokens = {}
    for text, conf in found:
        for token in BIB_TOKEN.findall(text):
            tokens[token] = max(conf, tokens.get(token, conf))
    return tokens


def read_bibs(crop: Image.Image, reader) -> dict[str, float]:
    if crop.height < OCR_MIN_HEIGHT:
        return {}
    if crop.height < OCR_UPSCALE_BELOW:
        crop = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
    return bib_tokens(reader(crop))


def ocr_bibs(db: sqlite3.Connection, collection: Path, reader=None, batcher=None) -> dict:
    pending = db.execute("""select p.id, p.photo_id, ph.relpath, p.x1, p.y1, p.x2, p.y2 from persons p
                            join photos ph on ph.id = p.photo_id
                            where p.ocr_at is null and ph.status = 'ok'
                            order by p.photo_id, p.id""").fetchall()
    counts = {"pending": len(pending), "persons": 0, "bibs": 0, "errors": 0, "ocr_errors": 0}
    log.info("ocr_bibs: %d pending", len(pending))
    if not pending:
        return counts
    reader = reader or models.read_text
    batcher = batcher or AdaptiveBatcher(OCR_BATCH)
    for chunk in batcher.chunks(pending):
        images, bad = {}, {}
        for _, photo_id, relpath, *_ in chunk:
            if photo_id not in images and photo_id not in bad:
                try:
                    images[photo_id] = models.load_image(collection / relpath)
                except Exception as e:
                    bad[photo_id] = error_text(e)
                    log.warning("unreadable image %s: %s", relpath, bad[photo_id])
        done = []
        for pid, photo_id, relpath, *box in chunk:
            if photo_id in images:
                try:
                    done.append((pid, read_bibs(models.crop(images[photo_id], box), reader)))
                except Exception as e:
                    counts["ocr_errors"] += 1
                    log.warning("ocr failed for person %d in %s, left pending: %s", pid, relpath, error_text(e))
        stamp = now()
        with db:
            db.executemany("update photos set status = 'error', error = ? where id = ?",
                           [(e, photo_id) for photo_id, e in bad.items()])
            for pid, bibs in done:
                db.executemany("insert into bibs(person_id, text, conf) values (?,?,?)",
                               [(pid, text, conf) for text, conf in bibs.items()])
                db.execute("update persons set ocr_at = ? where id = ?", (stamp, pid))
        counts["persons"] += len(done)
        counts["bibs"] += sum(len(b) for _, b in done)
        counts["errors"] += len(bad)
    log.info("ocr_bibs: %d persons read, %d bibs, %d photo errors, %d ocr errors (left pending)",
             counts["persons"], counts["bibs"], counts["errors"], counts["ocr_errors"])
    return counts
