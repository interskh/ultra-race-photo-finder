import sqlite3
from pathlib import Path

SCHEMA = """
create table if not exists photos(
  id integer primary key, relpath text not null unique, source_photo_id text,
  width integer, height integer, taken_at text, taken_ts real, camera text,
  photographer_uid text, photographer text, album text,
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
create table if not exists labels(
  person_id integer primary key references persons(id),
  label text not null check (label in ('me', 'not_me')), created_at text not null);
"""

INDEX_NAME = "index.sqlite"


def connect(collection: Path) -> sqlite3.Connection:
    db = sqlite3.connect(collection / INDEX_NAME, timeout=30)
    db.execute("pragma journal_mode=wal")
    db.execute("pragma foreign_keys=on")
    db.executescript(SCHEMA)
    return db
