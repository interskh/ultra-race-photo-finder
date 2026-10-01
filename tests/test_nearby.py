import pytest

from photofinder import cli, evaluate, nearby, search
from test_search import A, B, C, X, Y, Z, make_index, no_models
from test_web import ME, client, label, no_real_models, photo_ids  # noqa: F401

ROLL = {1: ("yipai:1", 100, "100"), 2: ("yipai:1", 100, "99"), 3: ("yipai:1", 102, "101"),
        4: ("yipai:1", 105, "102"), 5: ("yipai:1", 110, "103"), 6: ("yipai:1", None, "104"),
        7: ("yipai:1", 111, "105"), 8: ("yipai:2", 101, "200"), 9: ("yipai:2", 103, "201")}
NAMES = {"yipai:1": "阿光", "yipai:2": "Lens"}


def at(ts):
    return None if ts is None else f"2026-09-25 08:{ts // 60:02d}:{ts % 60:02d}"


def roll_index(tmp_path):
    c, conn, ids = make_index(tmp_path, [
        (1, (0, 0, 50, 100), [1, 0.05, 0, 0], X),
        (2, (0, 0, 50, 100), C, Z),
        (3, (0, 0, 50, 100), A, X),
        (3, (60, 0, 110, 100), B, Y),
        (4, (0, 0, 50, 100), B, Y),
        (4, (60, 0, 110, 100), [0.9, 0.1, 0, 0], X),
        (6, (0, 0, 50, 100), A, X),
        (7, (0, 0, 50, 100), [0.8, 0.2, 0, 0], X),
        (8, (0, 0, 50, 100), A, X),
        (9, (0, 0, 50, 100), B, Y),
        (9, (60, 0, 110, 100), [1, 0, 0.1, 0], X),
    ], photos=9)
    conn.executemany("update photos set photographer_uid = ?, photographer = ?, taken_ts = ?, taken_at = ?, "
                     "source_photo_id = ? where relpath = ?",
                     [(uid, NAMES[uid], ts, at(ts), src, f"{k}.jpg") for k, (uid, ts, src) in ROLL.items()])
    conn.executemany("insert into bibs(person_id, text, conf) values (?,?,?)",
                     [(ids[8][0], "2001", 0.9), (ids[4][0], "12001", 0.8)])
    conn.execute("update persons set ocr_at = '2026-09-28 00:00:00'")
    conn.commit()
    photo = {int(rel.split(".")[0]): i for rel, i in photo_ids(conn).items()}
    return c, conn, ids, photo


def near(api, profile=ME, **kw):
    res = api.post("/api/nearby", json={"profile_id": profile, **kw})
    assert res.status_code == 200, res.text
    return res.json()


def numbers(photo, results):
    back = {v: k for k, v in photo.items()}
    return [back[r["photo_id"]] for r in results]


def test_roll_orders_same_second_by_numeric_source_id_and_skips_untimed(tmp_path):
    _, conn, _, photo = roll_index(tmp_path)
    rolls, reasons = nearby.rolls(conn, [photo[3], photo[1], photo[6]], 5)
    assert [(p, off, gap) for p, off, gap, _ in rolls[photo[3]]] == [
        (photo[2], -2, -2), (photo[1], -1, -2), (photo[4], 1, 3), (photo[5], 2, 8), (photo[7], 3, 9)]
    assert [(p, off, gap) for p, off, gap, _ in nearby.rolls(conn, [photo[1]], 1)[0][photo[1]]] == [
        (photo[2], -1, 0), (photo[3], 1, 2)]
    assert photo[6] not in rolls and "capture time" in reasons[photo[6]]
    assert [p for p, *_ in nearby.rolls(conn, [photo[8]], 5)[0][photo[8]]] == [photo[9]]


def test_neighbors_endpoint_shows_raw_roll_with_best_match_to_me(tmp_path):
    c, _, ids, photo = roll_index(tmp_path)
    api = client(c)
    label(api, ids[3][0], "me")
    label(api, ids[1][0], "not_me")
    body = api.get(f"/api/photos/{photo[3]}/neighbors", params={"profile_id": ME, "span": 2}).json()
    assert body["anchor"]["photo_id"] == photo[3] and body["anchor"]["photographer"] == "阿光"
    assert body["confirmed"] is True and body["reference"] == [ids[3][0]] and body["reason"] is None
    got = body["neighbors"]
    assert numbers(photo, got) == [2, 1, 4, 5]
    assert [(n["offset"], n["gap_s"], n["same_second"]) for n in got] == [(-2, -2, False), (-1, -2, False),
                                                                           (1, 3, False), (2, 8, False)]
    assert [n["person_id"] for n in got] == [ids[2][0], None, ids[4][1], None]
    four = got[2]
    assert four["box"] == [60, 0, 110, 100] and four["bibs"] == [] and four["label"] is None
    assert four["source_photo_id"] == "102" and four["taken_at"] == at(105) and four["similarity"] > 0.9
    assert got[3]["box"] is None and got[3]["bibs"] == []


def test_neighbors_reference_person_overrides_me_and_without_reference_is_unconfirmed(tmp_path):
    c, _, ids, photo = roll_index(tmp_path)
    api = client(c)
    url = f"/api/photos/{photo[3]}/neighbors"
    body = api.get(url, params={"profile_id": ME, "span": 1}).json()
    assert body["confirmed"] is False and body["reference"] == []
    assert numbers(photo, body["neighbors"]) == [1, 4] and [n["person_id"] for n in body["neighbors"]] == [None, None]
    body = api.get(url, params={"profile_id": ME, "span": 1, "person_id": ids[3][1]}).json()
    assert body["confirmed"] is False and body["reference"] == [ids[3][1]]
    assert body["neighbors"][1]["person_id"] == ids[4][0]
    label(api, ids[3][0], "me")
    body = api.get(url, params={"profile_id": ME, "span": 1, "person_id": ids[3][0]}).json()
    assert body["confirmed"] is True and body["neighbors"][1]["person_id"] == ids[4][1]


def test_neighbors_of_untimed_photo_say_why_and_bad_params_are_rejected(tmp_path):
    c, _, ids, photo = roll_index(tmp_path)
    api = client(c)
    body = api.get(f"/api/photos/{photo[6]}/neighbors", params={"profile_id": ME}).json()
    assert body["neighbors"] == [] and "capture time" in body["reason"]
    url = f"/api/photos/{photo[3]}/neighbors"
    for span in (0, 6):
        assert api.get(url, params={"profile_id": ME, "span": span}).status_code == 400
    assert len(api.get(url, params={"profile_id": ME, "span": 5}).json()["neighbors"]) == 5
    assert api.get(url, params={"profile_id": ME, "person_id": ids[4][0]}).status_code == 400
    assert api.get(url, params={"profile_id": 99}).status_code == 404
    assert api.get("/api/photos/9999/neighbors", params={"profile_id": ME}).status_code == 404


def test_nearby_expands_me_photos_sorted_by_distance_then_time(tmp_path):
    c, _, ids, photo = roll_index(tmp_path)
    api = client(c)
    empty = near(api)
    assert (empty["results"], empty["count"], empty["warnings"]) == ([], 0, [])
    label(api, ids[3][0], "me")
    body = near(api, span=2)
    got = body["results"]
    assert numbers(photo, got) == [1, 4, 2, 5] and body["count"] == 4
    assert [(r["offset"], r["gap_s"]) for r in got] == [(-1, -2), (1, 3), (-2, -2), (2, 8)]
    assert {(r["anchor_photo_id"], r["anchor_person_id"]) for r in got} == {(photo[3], ids[3][0])}
    assert [r["person_id"] for r in got] == [ids[1][0], ids[4][1], ids[2][0], None]
    assert got[1]["box"] == [60, 0, 110, 100] and got[3]["box"] is None
    assert numbers(photo, near(api, span=1)["results"]) == [1, 4]


def test_nearby_hides_marked_photos_and_skips_not_me(tmp_path):
    c, _, ids, photo = roll_index(tmp_path)
    api = client(c)
    label(api, ids[3][0], "me")
    label(api, ids[4][0], "me")
    assert numbers(photo, near(api, span=1)["results"]) == [1, 5]
    label(api, ids[1][0], "not_me")
    assert numbers(photo, near(api, span=1)["results"]) == [5]
    label(api, ids[4][0], None)
    label(api, ids[4][1], "not_me")
    got = near(api, span=1)["results"]
    assert numbers(photo, got) == [4] and got[0]["person_id"] == ids[4][0]


def test_nearby_reaches_each_photo_once_from_its_nearest_anchor(tmp_path):
    c, _, ids, photo = roll_index(tmp_path)
    api = client(c)
    for pid in (ids[2][0], ids[4][1], ids[7][0]):
        label(api, pid, "me")
    got = near(api, span=2)["results"]
    assert numbers(photo, got) == [1, 5, 3]
    assert [(r["anchor_photo_id"], r["offset"], r["gap_s"], r["same_second"]) for r in got] == [
        (photo[2], 1, 0, True), (photo[7], -1, -1, False), (photo[4], -1, -3, False)]
    assert got[2]["anchor_person_id"] == ids[4][1]


def test_nearby_bib_anchors_use_the_bib_person_as_reference(tmp_path):
    c, conn, ids, photo = roll_index(tmp_path)
    api = client(c)
    got = near(api, anchors_bib=" 2001 ")["results"]
    assert numbers(photo, got) == [9]
    assert (got[0]["person_id"], got[0]["anchor_person_id"], got[0]["anchor_photo_id"]) == \
        (ids[9][1], ids[8][0], photo[8])
    assert near(api, anchors_bib="200")["results"] == []
    label(api, ids[8][0], "not_me")
    assert near(api, anchors_bib="2001")["results"] == []
    conn.execute("update persons set ocr_at = null where id != ?", (ids[8][0],))
    conn.commit()
    assert "ocr_bibs incomplete" in near(api, anchors_bib="2001")["warnings"][0]
    conn.execute("update persons set ocr_at = null")
    conn.commit()
    body = near(api, anchors_bib="2001")
    assert body["results"] == [] and "ocr_bibs" in body["warnings"][0]


def test_nearby_never_lists_a_bib_result_photo_even_when_its_bib_person_is_not_me(tmp_path):
    c, conn, ids, photo = roll_index(tmp_path)
    conn.execute("insert into bibs(person_id, text, conf) values (?, '2001', 0.7)", (ids[9][0],))
    conn.commit()
    api = client(c)
    label(api, ids[9][0], "not_me")
    bib_hits = {r["photo_id"] for r in api.post("/api/search", json={"profile_id": ME, "start_bib": "2001"}).json()["results"]}
    assert bib_hits == {photo[8], photo[9]}
    assert not bib_hits & {r["photo_id"] for r in near(api, anchors_bib="2001")["results"]}


def test_nearby_drops_neighbours_more_than_max_gap_away_but_the_strip_keeps_them(tmp_path):
    c, conn, ids, photo = roll_index(tmp_path)
    conn.executemany("update photos set taken_ts = ?, taken_at = ? where id = ?",
                     [(55, at(55), photo[3]), (104, at(104), photo[4])])
    conn.commit()
    api = client(c)
    label(api, ids[2][0], "me")
    got = near(api, span=2)["results"]
    assert [(n, r["offset"], r["gap_s"]) for n, r in zip(numbers(photo, got), got)] == [(1, 1, 0), (4, 2, 4)]
    assert nearby.MAX_GAP == 30
    strip = api.get(f"/api/photos/{photo[2]}/neighbors", params={"profile_id": ME, "span": 2}).json()["neighbors"]
    assert [(r["offset"], r["gap_s"]) for r in strip] == [(-1, -45), (1, 0), (2, 4)]


def test_nearby_applies_search_filters(tmp_path):
    c, _, ids, photo = roll_index(tmp_path)
    api = client(c)
    label(api, ids[3][0], "me")
    assert near(api, span=2, photographers=["Lens"])["results"] == []
    assert numbers(photo, near(api, span=2, anchors_bib="2001", photographers=["Lens"])["results"]) == [9]
    assert numbers(photo, near(api, span=2, anchors_bib="2001", photographers=["yipai:1"])["results"]) == [1, 4, 2, 5]
    assert numbers(photo, near(api, span=2, end="2026-09-25 08:01:44")["results"]) == [1, 2]
    got = near(api, span=2, bib="2001")["results"]
    assert numbers(photo, got) == [4] and got[0]["person_id"] == ids[4][0]
    assert api.post("/api/nearby", json={"profile_id": ME, "end": "soon"}).status_code == 400


def test_nearby_anchors_only_on_this_profiles_marks(tmp_path):
    c, _, ids, photo = roll_index(tmp_path)
    api = client(c)
    other = api.post("/api/profiles", json={"name": "Ann"}).json()["id"]
    label(api, ids[3][0], "me", other)
    label(api, ids[1][0], "not_me", other)
    assert near(api, span=1)["results"] == []
    got = near(api, other, span=1)["results"]
    assert numbers(photo, got) == [4]
    label(api, ids[3][0], "me")
    assert numbers(photo, near(api, span=1)["results"]) == [1, 4]


def test_nearby_rejects_bad_span_and_profile(tmp_path):
    c, _, _, _ = roll_index(tmp_path)
    api = client(c)
    for span in (0, 6):
        assert api.post("/api/nearby", json={"profile_id": ME, "span": span}).status_code == 400
    assert api.post("/api/nearby", json={"profile_id": 99}).status_code == 404


def test_search_exclude_photos_hides_without_shifting_ranks(tmp_path):
    c, _, ids, photo = roll_index(tmp_path)
    api = client(c)
    q = {"profile_id": ME, "persons": [ids[3][0]], "top": 3}
    full = api.post("/api/search", json=q).json()["results"]
    got = api.post("/api/search", json={**q, "exclude_photos": [full[0]["photo_id"]]}).json()["results"]
    assert [r["photo_id"] for r in got] == [r["photo_id"] for r in full[1:]] + [got[2]["photo_id"]]
    assert [r["rank"] for r in got] == [1, 2, 3] and full[0]["photo_id"] not in {r["photo_id"] for r in got}


def eval_roll(tmp_path):
    c, conn, ids = make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X), (2, (0, 0, 50, 100), [0.6, 0, 0.8, 0], X), (3, (0, 0, 50, 100), A, X),
        (4, (0, 0, 50, 100), C, Z), (5, (0, 0, 50, 100), [0, 0.3, 1, 0], Z)], photos=5)
    conn.executemany("update photos set photographer_uid = 'yipai:1', taken_ts = ?, taken_at = ? where relpath = ?",
                     [(k, at(k), f"{k}.jpg") for k in range(1, 6)])
    conn.executemany("insert into bibs(person_id, text, conf) values (?, '7', 1.0)", [(ids[k][0],) for k in (1, 2, 5)])
    conn.execute("update persons set ocr_at = '2026-09-28 00:00:00'")
    conn.commit()
    return c, conn


def test_eval_nearby_precision_and_recall_gain(tmp_path, monkeypatch):
    _, conn = eval_roll(tmp_path)
    persons = search.load_persons(conn)
    truth = evaluate.ground_truth(conn, "7", persons)
    monkeypatch.setattr(evaluate, "KS", (1, 1))
    rows = evaluate.nearby_bib(conn, persons, truth, {"osnet": 1.0})
    assert [(r.span, r.pairs, r.refs) for r in rows] == [(1, 4, 3), (2, 7, 3), (3, 10, 3)]
    assert [r.precision for r in rows] == pytest.approx([2 / 4, 2 / 7, 4 / 10])
    assert [r.r50 for r in rows] == [0, 0, 0]
    assert [r.near50 for r in rows] == pytest.approx([1 / 3, 1 / 3, 2 / 3])
    [m] = evaluate.mean_near([[rows[0]], [evaluate.NearRow(1, float("nan"), 0, 0.5, 1.0, 2)]])
    assert (m.precision, m.pairs, m.r50, m.near50, m.refs) == (0.5, 4, 0.25, pytest.approx(2 / 3), 5)


def test_cli_eval_prints_nearby_rows_as_lower_bounds(tmp_path, monkeypatch, capsys):
    c, _ = eval_roll(tmp_path)
    no_models(monkeypatch, tmp_path)
    cli.main(["eval", str(c), "--bib", "7"])
    lines = capsys.readouterr().out.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("bib 7: nearby shots"))
    assert "lower bounds" in lines[i] and lines[i + 1].split()[:2] == ["span", "prec"]
    assert [ln.split()[:3] for ln in lines[i + 2:i + 5]] == [["1", "0.500", "4"], ["2", "0.286", "7"],
                                                              ["3", "0.400", "10"]]
