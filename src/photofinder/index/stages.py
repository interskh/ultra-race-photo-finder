import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from PIL import Image

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
MANIFEST_NAME = "manifest.sqlite"
EXIF_IFD, DATETIME_ORIGINAL, MODEL = 0x8769, 0x9003, 0x0110
COMMIT_EVERY = 200

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


def load_manifest(collection: Path) -> dict[int, tuple]:
    path = collection / MANIFEST_NAME
    if not path.is_file():
        return {}
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
        rows = db.execute("""select p.photo_id, p.uid, g.nickname, t.name from photos p
                             left join photographers g on g.uid = p.uid
                             left join tags t on t.tag_id = p.tag_id""").fetchall()
    return {pid: (uid, nick, album) for pid, uid, nick, album in rows}


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
    return taken.strftime("%Y-%m-%d %H:%M:%S"), taken.timestamp(), camera


def scan(db: sqlite3.Connection, collection: Path) -> dict:
    manifest = load_manifest(collection)
    existing = {r for (r,) in db.execute("select relpath from photos")}
    counts = {"new": 0, "existing": 0, "errors": 0}
    for relpath in find_images(collection):
        if relpath in existing:
            counts["existing"] += 1
            continue
        stem = Path(relpath).stem
        try:
            uid, photographer, album = manifest.get(int(stem), (None, None, None))
        except ValueError:
            uid = photographer = album = None
        width = height = taken_at = taken_ts = camera = error = None
        status = "ok"
        try:
            with Image.open(collection / relpath) as img:
                width, height = img.size
                taken_at, taken_ts, camera = read_exif(img)
        except Exception as e:
            status, error = "error", f"{type(e).__name__}: {e}"
            counts["errors"] += 1
            log.warning("unreadable image %s: %s", relpath, error)
        db.execute("""insert into photos(relpath, source_photo_id, width, height, taken_at, taken_ts, camera,
                      photographer_uid, photographer, album, scanned_at, status, error)
                      values (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (relpath, stem, width, height, taken_at, taken_ts, camera, uid, photographer, album,
                    now(), status, error))
        counts["new"] += 1
        if counts["new"] % COMMIT_EVERY == 0:
            db.commit()
    db.commit()
    log.info("scan: %d new (%d errors), %d already indexed", counts["new"], counts["errors"], counts["existing"])
    return counts
