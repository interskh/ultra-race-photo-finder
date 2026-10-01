import sqlite3

import pytest

from photofinder import db
from test_db import columns, old_index
from test_nearby import near, numbers, roll_index
from test_search import A, B, C, X, Y, Z, make_index
from test_web import ME, client, label, more_index, no_real_models, ranked_photos  # noqa: F401


def batch(api, ids, profile=ME, undo=False):
    res = api.post("/api/labels/batch" + ("/undo" if undo else ""), json={"profile_id": profile, "person_ids": ids})
    assert res.status_code == 200, res.text
    return res.json()


def rows(c, profile=ME):
    with sqlite3.connect(c / db.INDEX_NAME) as conn:
        return conn.execute("select person_id, label, hidden from labels where profile_id = ? order by person_id",
                            (profile,)).fetchall()


def more(api, profile=ME, **kw):
    res = api.post("/api/search", json={"profile_id": profile, "mode": "more", **kw})
    assert res.status_code == 200, res.text
    return res.json()


def test_batch_marks_only_unlabelled_persons_in_photos_without_me(tmp_path):
    c, _, ids = more_index(tmp_path)
    api = client(c)
    label(api, ids[1][0], "me")
    label(api, ids[2][0], "not_me")
    label(api, ids[3][0], "me")
    body = batch(api, [ids[1][1], ids[2][0], ids[2][1], ids[3][0], ids[4][0], ids[4][0]])
    assert body["profile_id"] == ME and body["changed"] == [ids[2][1], ids[4][0]]
    assert body["skipped"] == [{"person_id": ids[1][1], "reason": "photo has a me person"},
                               {"person_id": ids[2][0], "reason": "already not_me"},
                               {"person_id": ids[3][0], "reason": "already me"}]
    got = {p: (lab, h) for p, lab, h in rows(c)}
    assert got[ids[2][1]] == ("not_me", 1) and got[ids[4][0]] == ("not_me", 1)
    assert got[ids[2][0]] == ("not_me", 0) and got[ids[1][0]] == ("me", 0) and ids[1][1] not in got


def test_batch_with_unknown_person_or_profile_is_rejected_and_writes_nothing(tmp_path):
    c, _, ids = more_index(tmp_path)
    api = client(c)
    for url in ("/api/labels/batch", "/api/labels/batch/undo"):
        res = api.post(url, json={"profile_id": ME, "person_ids": [ids[4][0], 9999]})
        assert (res.status_code == 404) == (url.endswith("batch")) and rows(c) == []
        assert api.post(url, json={"profile_id": 99, "person_ids": [ids[4][0]]}).status_code == 404
    assert rows(c) == []
    assert batch(api, []) == {"profile_id": ME, "changed": [], "skipped": []}


def test_batch_is_per_profile(tmp_path):
    c, _, ids = more_index(tmp_path)
    api = client(c)
    other = api.post("/api/profiles", json={"name": "Pat"}).json()["id"]
    label(api, ids[1][0], "me")
    label(api, ids[1][0], "me", other)
    batch(api, [ids[2][0]])
    assert rows(c, other) == [(ids[1][0], "me", 0)]
    assert more(api, other)["hidden"] == 0 and "2.jpg" in [r["relpath"] for r in more(api, other)["results"]]
    batch(api, [ids[2][1]], other)
    assert rows(c, ME)[-1][0] == ids[2][0] and more(api, other)["hidden"] == 1
    assert more(api, ME)["hidden"] == 1
    assert batch(api, [ids[2][1]], other)["skipped"][0]["reason"] == "already not_me"


def test_find_more_leaves_out_hidden_photo_even_with_second_unlabelled_person(tmp_path):
    c, _, ids = more_index(tmp_path)
    api = client(c)
    label(api, ids[1][0], "me")
    label(api, ids[2][0], "not_me")
    plain = more(api)
    assert "2.jpg" in [r["relpath"] for r in plain["results"]] and plain["hidden"] == 0
    label(api, ids[2][0], None)
    assert batch(api, [ids[2][0]])["changed"] == [ids[2][0]]
    body = more(api)
    assert [r["relpath"] for r in body["results"]] == ["3.jpg", "4.jpg"]
    assert [r["rank"] for r in body["results"]] == [1, 2] and body["hidden"] == 1
    paged = more(api, top=1, offset=1)
    assert [(r["relpath"], r["rank"]) for r in paged["results"]] == [("4.jpg", 2)]
    seen = more(api, seen=[body["results"][0]["photo_id"]])
    assert [(r["relpath"], r["rank"]) for r in seen["results"]] == [("4.jpg", 2)]


def test_hidden_photo_still_found_outside_find_more(tmp_path):
    c, _, ids = more_index(tmp_path)
    api = client(c)
    label(api, ids[1][0], "me")
    batch(api, [ids[2][0]])
    sim = api.post("/api/search", json={"profile_id": ME, "persons": [ids[1][0]]})
    assert "2.jpg" in ranked_photos(sim) and "hidden" not in sim.json()
    for mode in ("similar", "more"):
        res = api.post("/api/search", json={"profile_id": ME, "mode": mode, "persons": [ids[1][0]]})
        assert ("2.jpg" in ranked_photos(res)) == (mode == "similar")
    photo = next(r["photo_id"] for r in sim.json()["results"] if r["relpath"] == "2.jpg")
    detail = api.get(f"/api/photos/{photo}", params={"profile_id": ME}).json()
    assert [p["label"] for p in detail["persons"]] == ["not_me", None]


def test_bib_start_and_my_photos_ignore_hidden(tmp_path):
    c, conn, ids = more_index(tmp_path)
    conn.execute("insert into bibs(person_id, text, conf) values (?, '2001', 0.9)", (ids[2][0],))
    conn.execute("update persons set ocr_at = '2026-09-28 00:00:00'")
    conn.commit()
    api = client(c)
    label(api, ids[1][0], "me")
    batch(api, [ids[2][0]])
    res = api.post("/api/search", json={"profile_id": ME, "start_bib": "2001"})
    assert ranked_photos(res) == ["2.jpg"]
    assert [r["relpath"] for r in api.get("/api/me", params={"profile_id": ME}).json()["photos"]] == ["1.jpg"]


def test_nearby_hides_only_with_flag_and_neighbors_endpoint_is_raw(tmp_path):
    c, _, ids, photo = roll_index(tmp_path)
    api = client(c)
    label(api, ids[3][0], "me")
    assert numbers(photo, near(api, span=2)["results"]) == [1, 4, 2, 5]
    assert batch(api, [ids[4][0], ids[2][0]])["changed"] == [ids[4][0], ids[2][0]]
    assert numbers(photo, near(api, span=2)["results"]) == [1, 4, 5]
    assert numbers(photo, near(api, span=2, hide_hidden=False)["results"]) == [1, 4, 5]
    assert numbers(photo, near(api, span=2, hide_hidden=True)["results"]) == [1, 5]
    label(api, ids[4][0], None)
    assert numbers(photo, near(api, span=2, hide_hidden=True)["results"]) == [1, 4, 5]
    neigh = api.get(f"/api/photos/{photo[3]}/neighbors", params={"profile_id": ME, "span": 2}).json()["neighbors"]
    assert numbers(photo, neigh) == [2, 1, 4, 5]


def test_nearby_flag_on_batch_row_hides_unlike_person_level_not_me(tmp_path):
    c, _, ids, photo = roll_index(tmp_path)
    api = client(c)
    label(api, ids[3][0], "me")
    label(api, ids[4][1], "not_me")
    assert numbers(photo, near(api, span=1, hide_hidden=True)["results"]) == [1, 4]
    label(api, ids[4][1], None)
    batch(api, [ids[4][1]])
    assert numbers(photo, near(api, span=1, hide_hidden=True)["results"]) == [1]
    assert numbers(photo, near(api, span=1)["results"]) == [1, 4]


def test_undo_removes_only_rows_that_are_still_the_batch(tmp_path):
    c, _, ids = more_index(tmp_path)
    api = client(c)
    label(api, ids[1][0], "me")
    changed = batch(api, [ids[2][0], ids[2][1], ids[3][0], ids[4][0]])["changed"]
    label(api, ids[2][1], "me")
    label(api, ids[3][0], "not_me")
    label(api, ids[4][0], None)
    body = batch(api, changed, undo=True)
    assert body == {"profile_id": ME, "removed": [ids[2][0]], "kept": [ids[2][1], ids[3][0], ids[4][0]]}
    assert rows(c) == [(ids[1][0], "me", 0), (ids[2][1], "me", 0), (ids[3][0], "not_me", 0)]
    assert more(api)["hidden"] == 0


def test_undo_restores_find_more_exactly(tmp_path):
    c, _, ids = more_index(tmp_path)
    api = client(c)
    label(api, ids[1][0], "me")
    before = more(api)
    done = batch(api, [ids[2][0], ids[3][0], ids[4][0]])
    assert more(api)["results"] == []
    batch(api, done["changed"], undo=True)
    assert more(api)["results"] == before["results"] and more(api)["hidden"] == 0
    assert rows(c) == [(ids[1][0], "me", 0)]


def test_single_label_clears_hidden_flag_and_removal_unhides(tmp_path):
    c, _, ids = more_index(tmp_path)
    api = client(c)
    label(api, ids[1][0], "me")
    batch(api, [ids[2][0], ids[3][0], ids[4][0]])
    label(api, ids[2][0], "not_me")
    label(api, ids[3][0], "me")
    label(api, ids[4][0], None)
    assert rows(c) == [(ids[1][0], "me", 0), (ids[2][0], "not_me", 0), (ids[3][0], "me", 0)]
    assert more(api)["hidden"] == 0
    assert "2.jpg" in ranked_photos(api.post("/api/search", json={"profile_id": ME, "mode": "more"}))


def test_aggregates_count_batch_rows_and_profile_delete_removes_them(tmp_path):
    c, _, ids = more_index(tmp_path)
    api = client(c)
    other = api.post("/api/profiles", json={"name": "Pat"}).json()["id"]
    label(api, ids[1][0], "me", other)
    batch(api, [ids[2][0], ids[4][0]], other)
    assert api.get("/api/facets", params={"profile_id": other}).json()["labels"] == {"me": 1, "not_me": 2}
    assert api.delete(f"/api/profiles/{other}").json()["labels_removed"] == 3
    assert rows(c, other) == []


def test_fresh_schema_has_hidden_default_zero(tmp_path):
    c, conn, ids = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X)])
    assert columns(conn, "labels")[-1] == "hidden"
    conn.execute("insert into labels(profile_id, person_id, label, created_at) values (1, ?, 'me', 'x')", (ids[1][0],))
    assert conn.execute("select hidden from labels").fetchall() == [(0,)]


def pre_hidden_index(tmp_path):
    c, conn, ids = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X), (2, (0, 0, 9, 9), B, Y)])
    conn.close()
    raw = sqlite3.connect(c / db.INDEX_NAME)
    raw.executescript("""drop table labels;
      create table labels(profile_id integer not null references profiles(id),
        person_id integer not null references persons(id), label text not null check (label in ('me', 'not_me')),
        created_at text not null, primary key(profile_id, person_id));""")
    raw.executemany("insert into labels values (1, ?, ?, ?)", [(ids[1][0], "me", "t1"), (ids[2][0], "not_me", "t2")])
    raw.commit()
    raw.close()
    return c, ids


def test_migration_adds_hidden_to_existing_index_once_and_keeps_data(tmp_path):
    c, ids = pre_hidden_index(tmp_path)
    conn = db.connect(c)
    assert columns(conn, "labels") == ["profile_id", "person_id", "label", "created_at", "hidden"]
    assert not conn.in_transaction
    assert conn.execute("select * from labels order by person_id").fetchall() == [
        (1, ids[1][0], "me", "t1", 0), (1, ids[2][0], "not_me", "t2", 0)]
    conn.execute("update labels set hidden = 1 where person_id = ?", (ids[2][0],))
    conn.commit()
    conn.close()
    again = db.connect(c)
    assert again.execute("select person_id, hidden from labels order by person_id").fetchall() == [
        (ids[1][0], 0), (ids[2][0], 1)]


def test_migration_on_legacy_pre_profile_index_ends_with_hidden_column(tmp_path):
    c, rows_ = old_index(tmp_path)
    conn = db.connect(c)
    assert columns(conn, "labels") == ["profile_id", "person_id", "label", "created_at", "hidden"]
    assert conn.execute("select profile_id, person_id, label, created_at, hidden from labels order by person_id"
                        ).fetchall() == [(1, *r, 0) for r in rows_]
    conn.close()
    assert columns(db.connect(c), "labels").count("hidden") == 1


def test_failed_hidden_migration_rolls_back(tmp_path, monkeypatch):
    c, ids = pre_hidden_index(tmp_path)
    monkeypatch.setattr(db, "MIGRATE_HIDDEN", db.MIGRATE_HIDDEN + ["select no_such_function()"])
    with pytest.raises(sqlite3.OperationalError):
        db.connect(c)
    raw = sqlite3.connect(c / db.INDEX_NAME)
    assert "hidden" not in columns(raw, "labels") and raw.execute("select count(*) from labels").fetchone() == (2,)


def test_hidden_count_ignores_photos_that_are_me_photos_or_outside_the_filters(tmp_path):
    c, conn, ids = more_index(tmp_path)
    conn.execute("update photos set grp = case relpath when '2.jpg' then 'B' else 'A' end")
    conn.commit()
    api = client(c)
    label(api, ids[1][0], "me")
    batch(api, [ids[2][0], ids[4][0]])
    assert more(api)["hidden"] == 2
    assert more(api, groups=["B"])["hidden"] == 1
    assert more(api, groups=["A"])["hidden"] == 1
    assert more(api, groups=["C"])["hidden"] == 0
    label(api, ids[2][1], "me")
    assert more(api)["hidden"] == 1
