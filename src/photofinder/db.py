import sqlite3
from pathlib import Path

SCHEMA = """
create table if not exists photos(
  id integer primary key, relpath text not null unique, source_photo_id text,
  width integer, height integer, taken_at text, taken_ts real, camera text,
  photographer_uid text, photographer text, album text, album_key text, grp text,
  scanned_at text, detected_at text, scene_done_at text,
  status text not null default 'ok', error text);
create table if not exists persons(
  id integer primary key, photo_id integer not null references photos(id),
  x1 real, y1 real, x2 real, y2 real, conf real, embedded_at text, ocr_at text);
create index if not exists persons_photo on persons(photo_id);
create table if not exists emb_person_osnet(person_id integer primary key references persons(id), v blob not null);
create table if not exists emb_person_siglip(person_id integer primary key references persons(id), v blob not null);
create table if not exists emb_scene_siglip(photo_id integer primary key references photos(id), v blob not null);
create table if not exists bibs(person_id integer not null references persons(id), text text not null, conf real);
create index if not exists bibs_person on bibs(person_id);
"""
LABELS = """create table if not exists {}(
  profile_id integer not null references profiles(id), person_id integer not null references persons(id),
  label text not null check (label in ('me', 'not_me')), created_at text not null,
  hidden integer not null default 0,
  primary key(profile_id, person_id))"""
PROFILES = "create table if not exists profiles(id integer primary key, name text unique not null, created_at text not null)"
SCHEMA += PROFILES + ";\n" + LABELS.format("labels") + ";\n"
NOW = "strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"
DEFAULT_PROFILE = "Me"
MIGRATE = [
    PROFILES,
    f"insert into profiles(name, created_at) values ('{DEFAULT_PROFILE}', {NOW})",
    LABELS.format("labels_new"),
    "insert into labels_new(profile_id, person_id, label, created_at) "
    f"select (select id from profiles where name = '{DEFAULT_PROFILE}'), person_id, label, created_at from labels",
    "drop table labels",
    "alter table labels_new rename to labels",
]
MIGRATE_PHOTOS = [
    "alter table photos add column album_key text",
    "alter table photos add column grp text",
    "update photos set grp = album, album = null",
]
MIGRATE_HIDDEN = ["alter table labels add column hidden integer not null default 0"]

INDEX_NAME = "index.sqlite"


def old_labels(db) -> bool:
    cols = [r[1] for r in db.execute("pragma table_info(labels)")]
    return bool(cols) and "profile_id" not in cols


def no_hidden(db) -> bool:
    cols = [r[1] for r in db.execute("pragma table_info(labels)")]
    return bool(cols) and "hidden" not in cols


def old_photos(db) -> bool:
    cols = [r[1] for r in db.execute("pragma table_info(photos)")]
    return bool(cols) and "grp" not in cols


def once(db, needed, sqls):
    if not needed(db):
        return
    db.execute("begin immediate")
    try:
        if needed(db):
            for sql in sqls:
                db.execute(sql)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def migrate(db):
    once(db, old_labels, MIGRATE)
    once(db, no_hidden, MIGRATE_HIDDEN)
    once(db, old_photos, MIGRATE_PHOTOS)


def connect(collection: Path) -> sqlite3.Connection:
    db = sqlite3.connect(collection / INDEX_NAME, timeout=30)
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    migrate(db)
    db.executescript(SCHEMA)
    if not db.execute("select 1 from profiles limit 1").fetchone():
        db.execute(f"insert or ignore into profiles(name, created_at) values ('{DEFAULT_PROFILE}', {NOW})")
        db.commit()
    return db
