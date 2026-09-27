import numpy as np
import pytest
from PIL import Image

from photofinder import cli, db, models, search
from photofinder.index.stages import scan, to_blob

A, B, C = [1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0]
X, Y, Z = [1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]


def make_index(tmp_path, persons, photos=3, embed=True):
    c = tmp_path / "coll"
    c.mkdir()
    for i in range(1, photos + 1):
        Image.new("RGB", (200, 300), (40 * i, 80, 120)).save(c / f"{i}.jpg", "JPEG")
    conn = db.connect(c)
    scan(conn, c)
    ids = {}
    for photo, box, osnet, siglip in persons:
        pid = conn.execute("insert into persons(photo_id, x1, y1, x2, y2, conf) "
                           "select id, ?, ?, ?, ?, 0.9 from photos where relpath = ?",
                           (*box, f"{photo}.jpg")).lastrowid
        ids.setdefault(photo, []).append(pid)
        if embed:
            conn.execute("insert into emb_person_osnet values (?,?)", (pid, to_blob(osnet)))
            conn.execute("insert into emb_person_siglip values (?,?)", (pid, to_blob(siglip)))
    conn.commit()
    return c, conn, ids


def cos(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    return a @ b / np.linalg.norm(a) / np.linalg.norm(b)


def test_nearest_person_ranks_its_photo_first(tmp_path):
    _, conn, _ = make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (2, (10, 10, 60, 110), [0.1, 1, 0, 0], [0.1, 1, 0]),
        (3, (0, 0, 40, 90), [0.5, 0.5, 0, 0], [0.5, 0.5, 0]),
    ])
    results = search.search(conn, {"osnet": np.array([B]), "siglip": np.array([Y])})
    assert [r.relpath for r in results] == ["2.jpg", "3.jpg", "1.jpg"]
    assert [r.rank for r in results] == [1, 2, 3]
    assert results[0].box == (10, 10, 60, 110)
    assert results[0].score == pytest.approx(cos([0.1, 1, 0, 0], B) / 2 + cos([0.1, 1, 0], Y) / 2, abs=2e-3)


def test_photo_with_two_persons_appears_once_with_best_box(tmp_path):
    _, conn, ids = make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (1, (100, 0, 180, 250), B, Y),
        (2, (0, 0, 40, 90), [0.3, 1, 0, 0], [0.3, 1, 0]),
    ])
    results = search.search(conn, {"osnet": np.array([B]), "siglip": np.array([Y])})
    assert [r.relpath for r in results] == ["1.jpg", "2.jpg"]
    assert results[0].person_id == ids[1][1]
    assert results[0].box == (100, 0, 180, 250)


def test_top_k_limits_photos(tmp_path):
    _, conn, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X), (2, (0, 0, 9, 9), B, Y), (3, (0, 0, 9, 9), C, Z)])
    assert len(search.search(conn, {"osnet": np.array([A])}, top=2)) == 2


def test_missing_term_renormalizes_weights(tmp_path):
    v = [0.6, 0.8, 0.3, 0.1]
    _, conn, _ = make_index(tmp_path, [(1, (0, 0, 50, 100), v, X)])
    q = np.array([[2.0, 1.0, 0.0, 0.5]])
    [r] = search.search(conn, {"osnet": q})
    assert r.score == pytest.approx(cos(v, q[0]), abs=2e-3)
    [r] = search.search(conn, {"osnet": q, "siglip": None})
    assert r.score == pytest.approx(cos(v, q[0]), abs=2e-3)


def test_score_takes_max_over_refs(tmp_path):
    _, conn, ids = make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (2, (0, 0, 50, 100), C, Z),
        (3, (0, 0, 50, 100), [0.2, 0.2, 1, 0], [0.2, 0.2, 1]),
    ])
    persons = search.load_persons(conn)
    refs = search.person_refs(persons, [ids[1][0], ids[2][0]])
    scores = dict(zip(persons.ids, search.score(persons, refs)))
    assert scores[ids[1][0]] == pytest.approx(1, abs=2e-3)
    assert scores[ids[2][0]] == pytest.approx(1, abs=2e-3)
    assert scores[ids[3][0]] == pytest.approx(cos([0.2, 0.2, 1, 0], C) / 2 + cos([0.2, 0.2, 1], Z) / 2, abs=2e-3)


def test_exclude_drops_photo(tmp_path):
    _, conn, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X), (2, (0, 0, 9, 9), B, Y)])
    photo_1 = conn.execute("select id from photos where relpath = '1.jpg'").fetchone()[0]
    results = search.search(conn, {"osnet": np.array([A])}, exclude=[photo_1])
    assert [r.relpath for r in results] == ["2.jpg"]


def test_no_embeddings_names_missing_stage(tmp_path):
    _, conn, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X)], embed=False)
    with pytest.raises(search.MissingEmbeddings, match="embed_persons") as e:
        search.load_persons(conn)
    assert "photofinder index" in str(e.value)


QUERY_BOXES = [(10.0, 10.0, 40.0, 60.0, 0.9), (20.0, 30.0, 140.0, 280.0, 0.8)]


class Fakes:
    def __init__(self, monkeypatch, boxes=QUERY_BOXES, osnet=B, siglip=Y):
        self.detected, self.embedded = [], []
        self.boxes, self.osnet, self.siglip = boxes, osnet, siglip
        monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
        monkeypatch.setattr(models, "detect_persons", self.detect)
        monkeypatch.setattr(models, "embed_crops", self.embed)

    def detect(self, images):
        self.detected.append([img.size for img in images])
        return [list(self.boxes) for _ in images]

    def embed(self, crops):
        self.embedded.append([c.size for c in crops])
        n = len(crops)
        return np.array([self.osnet] * n, np.float32), np.array([self.siglip] * n, np.float32)


def search_index(tmp_path):
    return make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (2, (10, 20, 110, 220), B, Y),
        (3, (0, 0, 40, 90), [0.5, 0.5, 0, 0], [0.5, 0.5, 0]),
    ])


def query_photo(tmp_path):
    q = tmp_path / "query.jpg"
    Image.new("RGB", (160, 300), "white").save(q, "JPEG")
    return q


def run(capsys, *argv):
    with pytest.raises(SystemExit) as e:
        cli.main(["search", *map(str, argv)])
    assert e.value.code not in (0, None)
    return str(e.value.code), capsys.readouterr().out


def test_cli_box_out_of_range(tmp_path, monkeypatch, capsys):
    c, _, _ = search_index(tmp_path)
    fakes = Fakes(monkeypatch)
    msg, _ = run(capsys, c, "--photo", query_photo(tmp_path), "--box", 2, "--out", tmp_path / "s.jpg")
    assert "--box 2" in msg and "0..1" in msg and "\n" not in msg
    assert fakes.embedded == []
    assert not (tmp_path / "s.jpg").exists()


def test_cli_no_person_suggests_whole(tmp_path, monkeypatch, capsys):
    c, _, _ = search_index(tmp_path)
    fakes = Fakes(monkeypatch, boxes=[])
    msg, _ = run(capsys, c, "--photo", query_photo(tmp_path))
    assert "no person" in msg and "--whole" in msg
    assert fakes.embedded == []


def test_cli_boxes_sorted_by_area_and_default_box_is_largest(tmp_path, monkeypatch, capsys):
    c, _, _ = search_index(tmp_path)
    fakes = Fakes(monkeypatch)
    cli.main(["search", str(c), "--photo", str(query_photo(tmp_path)), "--out", str(tmp_path / "s.jpg")])
    out = capsys.readouterr().out
    assert "box 0: (20,30,140,280)" in out and "box 1: (10,10,40,60)" in out
    assert fakes.embedded == [[(120, 250)]]

    cli.main(["search", str(c), "--photo", str(query_photo(tmp_path)), "--box", "1", "--out", str(tmp_path / "s.jpg")])
    assert fakes.embedded[-1] == [(30, 50)]


def test_cli_whole_skips_detection(tmp_path, monkeypatch, capsys):
    c, _, _ = search_index(tmp_path)
    fakes = Fakes(monkeypatch)
    cli.main(["search", str(c), "--photo", str(query_photo(tmp_path)), "--whole", "--out", str(tmp_path / "s.jpg")])
    assert fakes.detected == []
    assert fakes.embedded == [[(160, 300)]]


def test_cli_prints_ranking_and_writes_contact_sheet(tmp_path, monkeypatch, capsys):
    c, _, _ = search_index(tmp_path)
    Fakes(monkeypatch)
    out = tmp_path / "sheets" / "s.jpg"
    cli.main(["search", str(c), "--photo", str(query_photo(tmp_path)), "--top", "2", "--out", str(out)])
    lines = capsys.readouterr().out.splitlines()
    ranked = [ln.split() for ln in lines if ln.lstrip()[:1].isdigit()]
    assert [(r[0], r[2]) for r in ranked] == [("1", "2.jpg"), ("2", "3.jpg")]
    assert "box=(10,20,110,220)" in ranked[0]
    assert f"contact sheet: {out}" in lines
    with Image.open(out) as img:
        assert img.format == "JPEG"
        img.verify()


def test_cli_default_sheet_goes_to_exports_outside_collection(tmp_path, monkeypatch, capsys):
    c, _, _ = search_index(tmp_path)
    Fakes(monkeypatch)
    monkeypatch.setattr(cli.config, "DATA_ROOT", tmp_path / "data")
    before = sorted(p.name for p in c.rglob("*.jpg"))
    cli.main(["search", str(c), "--photo", str(query_photo(tmp_path))])
    [sheet] = (tmp_path / "data" / "exports").glob("*.jpg")
    assert sheet.name.startswith("coll-search-")
    assert f"contact sheet: {sheet}" in capsys.readouterr().out
    assert not sheet.resolve().is_relative_to(c.resolve())
    assert sorted(p.name for p in c.rglob("*.jpg")) == before


def test_cli_excludes_query_when_it_is_indexed(tmp_path, monkeypatch, capsys):
    c, _, _ = search_index(tmp_path)
    Fakes(monkeypatch)
    cli.main(["search", str(c), "--photo", str(c / "2.jpg"), "--out", str(tmp_path / "s.jpg")])
    out = capsys.readouterr().out
    assert "query photo is indexed as 2.jpg; excluded from results" in out
    assert " 2.jpg " not in out
    assert " 3.jpg " in out


def test_cli_excludes_query_given_as_symlink_target(tmp_path, monkeypatch, capsys):
    c, conn, _ = search_index(tmp_path)
    target = tmp_path / "elsewhere" / "4.jpg"
    target.parent.mkdir()
    Image.new("RGB", (200, 300), "red").save(target, "JPEG")
    (c / "4.jpg").symlink_to(target)
    scan(conn, c)
    conn.execute("insert into persons(photo_id, x1, y1, x2, y2, conf) "
                 "select id, 0, 0, 90, 200, 0.9 from photos where relpath = '4.jpg'")
    pid = conn.execute("select max(id) from persons").fetchone()[0]
    conn.execute("insert into emb_person_osnet values (?,?)", (pid, to_blob(B)))
    conn.execute("insert into emb_person_siglip values (?,?)", (pid, to_blob(Y)))
    conn.commit()
    Fakes(monkeypatch)
    cli.main(["search", str(c), "--photo", str(target), "--out", str(tmp_path / "s.jpg")])
    out = capsys.readouterr().out
    assert "query photo is indexed as 4.jpg; excluded from results" in out
    assert " 4.jpg " not in out


def test_cli_missing_index_does_not_create_one(tmp_path, monkeypatch, capsys):
    c = tmp_path / "coll"
    c.mkdir()
    Fakes(monkeypatch)
    msg, _ = run(capsys, c, "--photo", query_photo(tmp_path))
    assert "photofinder index" in msg
    assert not (c / "index.sqlite").exists()


def test_cli_no_embeddings_exits_naming_stage(tmp_path, monkeypatch, capsys):
    c, _, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X)], embed=False)
    fakes = Fakes(monkeypatch)
    msg, _ = run(capsys, c, "--photo", query_photo(tmp_path))
    assert "embed_persons" in msg
    assert fakes.detected == []


def test_cli_missing_collection(tmp_path, monkeypatch, capsys):
    Fakes(monkeypatch)
    msg, _ = run(capsys, tmp_path / "nope", "--photo", query_photo(tmp_path))
    assert "nope" in msg

