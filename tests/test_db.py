import sqlite3

import pytest

from photofinder import db
from test_search import A, B, C, X, Y, Z, make_index

OLD_LABELS = """drop table labels; drop table profiles;
create table labels(
  person_id integer primary key references persons(id),
  label text not null check (label in ('me', 'not_me')), created_at text not null);"""


def old_index(tmp_path, orphan=False):
    c, conn, ids = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X), (2, (0, 0, 9, 9), B, Y), (3, (0, 0, 9, 9), C, Z)])
    conn.close()
    raw = sqlite3.connect(c / db.INDEX_NAME)
    raw.executescript(OLD_LABELS)
    rows = [(ids[1][0], "me", "2026-09-20 10:00:00"), (ids[2][0], "not_me", "2026-09-21 11:00:00"),
            (ids[3][0], "me", "2026-09-22 12:00:00")] + ([(999, "me", "2026-09-23 13:00:00")] if orphan else [])
    raw.executemany("insert into labels values (?,?,?)", rows)
    raw.commit()
    raw.close()
    return c, rows


def columns(conn, table):
    return [r[1] for r in conn.execute(f"pragma table_info({table})")]


def tables(conn):
    return {r[0] for r in conn.execute("select name from sqlite_master where type = 'table'")}


def test_fresh_index_has_profile_labels_and_one_default_profile(tmp_path):
    c, conn, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X)])
    assert columns(conn, "labels") == ["profile_id", "person_id", "label", "created_at"]
    assert conn.execute("select id, name from profiles").fetchall() == [(1, "Me")]
    conn.execute("update profiles set name = 'Alex'")
    conn.commit()
    assert db.connect(c).execute("select id, name from profiles").fetchall() == [(1, "Alex")]
    conn.execute("delete from profiles")
    conn.commit()
    db.connect(c).close()
    assert sqlite3.connect(c / db.INDEX_NAME).execute("select name from profiles").fetchall() == [("Me",)]


def test_migration_moves_old_labels_under_me_once(tmp_path):
    c, rows = old_index(tmp_path)
    conn = db.connect(c)
    assert conn.execute("select id, name from profiles").fetchall() == [(1, "Me")]
    assert conn.execute("select profile_id, person_id, label, created_at from labels order by person_id").fetchall() \
        == [(1, *r) for r in rows]
    assert columns(conn, "labels") == ["profile_id", "person_id", "label", "created_at"]
    assert {r[2] for r in conn.execute("pragma foreign_key_list(labels)")} == {"persons", "profiles"}
    assert "labels_new" not in tables(conn) and not conn.in_transaction
    conn.close()
    again = db.connect(c)
    assert again.execute("select id, name from profiles").fetchall() == [(1, "Me")]
    assert again.execute("select profile_id, person_id, label, created_at from labels order by person_id").fetchall() \
        == [(1, *r) for r in rows]


def assert_untouched(conn, rows):
    assert columns(conn, "labels") == ["person_id", "label", "created_at"]
    assert conn.execute("select * from labels order by person_id").fetchall() == rows
    assert not {"profiles", "labels_new"} & tables(conn)


def test_failed_migration_after_drop_rolls_back_everything(tmp_path, monkeypatch):
    c, rows = old_index(tmp_path)
    monkeypatch.setattr(db, "MIGRATE", db.MIGRATE[:-1] + ["select no_such_function()"] + db.MIGRATE[-1:])
    conn = sqlite3.connect(c / db.INDEX_NAME)
    conn.execute("pragma foreign_keys=on")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn)
    assert not conn.in_transaction
    assert_untouched(conn, rows)


def test_orphan_label_fails_migration_and_keeps_old_table(tmp_path):
    c, rows = old_index(tmp_path, orphan=True)
    with pytest.raises(sqlite3.IntegrityError):
        db.connect(c)
    assert_untouched(sqlite3.connect(c / db.INDEX_NAME), sorted(rows))
