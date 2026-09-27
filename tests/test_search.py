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
    def __init__(self, monkeypatch, boxes=QUERY_BOXES, osnet=B, siglip=Y, texts=None):
        self.detected, self.embedded, self.encoded = [], [], []
        self.boxes, self.osnet, self.siglip, self.texts = boxes, osnet, siglip, texts or {}
        monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
        monkeypatch.setattr(models, "detect_persons", self.detect)
        monkeypatch.setattr(models, "embed_crops", self.embed)
        monkeypatch.setattr(models, "encode_text", self.encode)

    def encode(self, texts):
        self.encoded.append(list(texts))
        return models.l2norm([self.texts[t] for t in texts])

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


def test_find_photo_matches_case_variant_of_indexed_path(tmp_path):
    c, conn, _ = make_index(tmp_path, [])
    Image.new("RGB", (20, 30), "red").save(c / "x.jpg", "JPEG")
    scan(conn, c)
    query = c / "X.JPG"
    if not query.exists():
        pytest.skip("case-sensitive filesystem")
    assert search.find_photo(conn, c, query)[1] == "x.jpg"
    assert search.find_photo(conn, c, c / "1.jpg")[1] == "1.jpg"
    assert search.find_photo(conn, c, tmp_path / "missing.jpg") is None


def test_contact_sheet_scales_wide_tile_to_fit(tmp_path):
    pano = Image.new("RGB", (4000, 400), (255, 0, 0))
    pano.paste((0, 0, 255), (3800, 0, 4000, 400))
    out = tmp_path / "s.jpg"
    search.contact_sheet([(pano, "pano")], out)
    with Image.open(out) as sheet:
        r, g, b = sheet.convert("RGB").getpixel((sheet.width - 10, 40))
    assert b > 200 and r < 60


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



def add_scenes(conn, scenes):
    for photo, v in scenes:
        conn.execute("insert into emb_scene_siglip select id, ? from photos where relpath = ?", (to_blob(v), f"{photo}.jpg"))
    conn.commit()


def scene_index(tmp_path):
    c, conn, ids = make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (1, (60, 0, 110, 100), B, Y),
        (2, (0, 0, 50, 100), C, Z),
        (3, (0, 0, 50, 100), [0, 0, 0, 1.0], Y),
        (4, (0, 0, 50, 100), A, Z),
    ], photos=5)
    add_scenes(conn, [(5, X), (3, [1.0, 0.1, 0]), (2, [-0.3, 1, 0]), (1, [0.5, 1, 0])])
    return c, conn, ids


def test_scene_only_ranks_persons_by_their_photo_scene(tmp_path):
    _, conn, ids = scene_index(tmp_path)
    results = search.search(conn, {"scene": np.array([X])})
    assert [r.relpath for r in results] == ["3.jpg", "1.jpg", "4.jpg", "2.jpg"]
    assert [round(r.score, 3) for r in results] == pytest.approx(
        [cos([1, 0.1, 0], X), cos([0.5, 1, 0], X), 0, cos([-0.3, 1, 0], X)], abs=2e-3)
    persons = search.load_scenes(conn, search.load_persons(conn))
    scores = dict(zip(persons.ids, search.score(persons, {"scene": np.array([X])})))
    assert scores[ids[1][0]] == scores[ids[1][1]]


def test_text_term_scores_person_crop_vectors(tmp_path):
    _, conn, _ = make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, Z),
        (2, (0, 0, 50, 100), B, X),
        (3, (0, 0, 50, 100), C, [0.5, 0.5, 0]),
    ])
    add_scenes(conn, [(1, X), (2, Z), (3, Y)])
    results = search.search(conn, {"text": np.array([X])})
    assert [r.relpath for r in results] == ["2.jpg", "3.jpg", "1.jpg"]
    assert results[1].score == pytest.approx(cos([0.5, 0.5, 0], X), abs=2e-3)


def test_scene_query_without_scene_embeddings_names_stage(tmp_path):
    _, conn, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X), (2, (0, 0, 9, 9), B, Y)])
    with pytest.raises(search.MissingEmbeddings, match="embed_scenes") as e:
        search.search(conn, {"osnet": np.array([A]), "scene": np.array([X])})
    assert "photofinder index" in str(e.value)
    assert [r.relpath for r in search.search(conn, {"osnet": np.array([A]), "text": np.array([Y])})] == [
        "1.jpg", "2.jpg"]


@pytest.mark.parametrize("weights", [search.WEIGHTS, {"osnet": 1.0, "siglip": 0.0, "text": 2.0, "scene": 3.0}])
def test_photo_text_and_scene_terms_renormalize(tmp_path, weights):
    o, s, sc = [0.6, 0.8, 0.3, 0.1], [0.2, 0.9, 0.4], [0.7, 0.1, 0.7]
    _, conn, _ = make_index(tmp_path, [(1, (0, 0, 50, 100), o, s)])
    add_scenes(conn, [(1, sc)])
    qo, qt, qs = [2.0, 1.0, 0.0, 0.5], [0.1, 1.0, 0.3], [1.0, 0.2, 0.0]
    [r] = search.search(conn, {"osnet": np.array([qo]), "text": np.array([qt]), "scene": np.array([qs])},
                        weights=weights)
    parts = [(weights["osnet"], cos(o, qo)), (weights["text"], cos(s, qt)), (weights["scene"], cos(sc, qs))]
    assert r.score == pytest.approx(sum(w * c for w, c in parts) / sum(w for w, _ in parts), abs=2e-3)


def test_cli_without_any_query_exits_with_one_line(tmp_path, monkeypatch, capsys):
    c, _, _ = search_index(tmp_path)
    fakes = Fakes(monkeypatch)
    msg, _ = run(capsys, c, "--out", tmp_path / "s.jpg")
    assert "--photo" in msg and "--text" in msg and "--scene" in msg and "\n" not in msg
    assert fakes.detected == fakes.embedded == fakes.encoded == []
    assert not (tmp_path / "s.jpg").exists()


def test_cli_text_only_loads_no_person_models(tmp_path, monkeypatch, capsys):
    c, _, _ = search_index(tmp_path)
    fakes = Fakes(monkeypatch, texts={"orange vest": Y})
    out = tmp_path / "s.jpg"
    cli.main(["search", str(c), "--text", "orange vest", "--out", str(out)])
    ranked = [ln.split() for ln in capsys.readouterr().out.splitlines() if ln.lstrip()[:1].isdigit()]
    assert [r[2] for r in ranked] == ["2.jpg", "3.jpg", "1.jpg"]
    assert fakes.encoded == [["orange vest"]]
    assert fakes.detected == fakes.embedded == []
    with Image.open(out) as img:
        img.verify()


def test_cli_scene_only_ranks_by_scene(tmp_path, monkeypatch, capsys):
    c, _, _ = scene_index(tmp_path)
    fakes = Fakes(monkeypatch, texts={"雪山": X})
    cli.main(["search", str(c), "--scene", "雪山", "--out", str(tmp_path / "s.jpg")])
    ranked = [ln.split() for ln in capsys.readouterr().out.splitlines() if ln.lstrip()[:1].isdigit()]
    assert [r[2] for r in ranked] == ["3.jpg", "1.jpg", "4.jpg", "2.jpg"]
    assert fakes.encoded == [["雪山"]]
    assert fakes.detected == fakes.embedded == []


def test_cli_scene_without_scene_embeddings_exits_before_models(tmp_path, monkeypatch, capsys):
    c, _, _ = search_index(tmp_path)
    fakes = Fakes(monkeypatch, texts={"mountain": X})
    msg, _ = run(capsys, c, "--photo", query_photo(tmp_path), "--scene", "mountain")
    assert "embed_scenes" in msg
    assert fakes.detected == fakes.embedded == fakes.encoded == []


def test_cli_photo_with_text_and_scene_encodes_both_texts(tmp_path, monkeypatch, capsys):
    c, _, _ = scene_index(tmp_path)
    fakes = Fakes(monkeypatch, osnet=A, siglip=X, texts={"red": Y, "arch": Z})
    cli.main(["search", str(c), "--photo", str(query_photo(tmp_path)), "--text", "red", "--scene", "arch",
              "--out", str(tmp_path / "s.jpg")])
    assert fakes.encoded == [["red", "arch"]]
    assert len(fakes.embedded) == 1
    ranked = [ln.split() for ln in capsys.readouterr().out.splitlines() if ln.lstrip()[:1].isdigit()]
    assert ranked[0][2] == "1.jpg"


def filter_index(tmp_path):
    c, conn, ids = make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (1, (60, 0, 110, 100), [0.9, 0.1, 0, 0], X),
        (2, (0, 0, 50, 100), [0.8, 0.2, 0, 0], X),
        (3, (0, 0, 50, 100), [0.7, 0.3, 0, 0], X),
        (4, (0, 0, 50, 100), [0.6, 0.4, 0, 0], X),
    ], photos=4)
    conn.executemany("update photos set taken_at = ?, photographer_uid = ?, photographer = ?, album = ? "
                     "where relpath = ?", [
                         ("2026-09-25 08:00:00", "u1", "阿光", "9.25 赛事", "1.jpg"),
                         ("2026-09-25 09:30:00", "u2", "Lens", "9.25 赛事", "2.jpg"),
                         ("2026-09-25 10:00:00", "u3", None, "定妆照", "3.jpg"),
                         (None, "u1", "阿光", "9.25 赛事", "4.jpg")])
    conn.executemany("insert into bibs(person_id, text, conf) values (?,?,?)",
                     [(ids[1][1], "2001", 1.0), (ids[2][0], "12001", 0.5), (ids[3][0], "2100", 1.0)])
    conn.execute("update persons set ocr_at = '2026-09-28 00:00:00'")
    conn.commit()
    return c, conn, ids


def ranked(conn, **filters):
    return [r.relpath for r in search.search(conn, {"osnet": np.array([A])}, filters=search.Filters(**filters))]


def test_no_filters_returns_everything_including_null_time(tmp_path):
    _, conn, _ = filter_index(tmp_path)
    assert ranked(conn) == ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]
    assert not search.Filters()


def test_time_window_is_inclusive_and_drops_null_time_photos(tmp_path):
    _, conn, _ = filter_index(tmp_path)
    assert ranked(conn, start="2026-09-25 09:30:00") == ["2.jpg", "3.jpg"]
    assert ranked(conn, end="2026-09-25 09:30:00") == ["1.jpg", "2.jpg"]
    assert ranked(conn, start="2026-09-25 08:00:01", end="2026-09-25 09:59:59") == ["2.jpg"]


def test_photographer_matches_nickname_or_uid_and_repeats(tmp_path):
    _, conn, _ = filter_index(tmp_path)
    assert ranked(conn, photographers=("阿光",)) == ["1.jpg", "4.jpg"]
    assert ranked(conn, photographers=("u3",)) == ["3.jpg"]
    assert ranked(conn, photographers=("Lens", "u3")) == ["2.jpg", "3.jpg"]
    assert ranked(conn, photographers=("阿",)) == []


def test_album_is_exact_and_repeatable(tmp_path):
    _, conn, _ = filter_index(tmp_path)
    assert ranked(conn, albums=("定妆照",)) == ["3.jpg"]
    assert ranked(conn, albums=("定妆照", "9.25 赛事")) == ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]
    assert ranked(conn, albums=("9.25",)) == []


def test_bib_substring_keeps_only_matching_persons(tmp_path):
    _, conn, ids = filter_index(tmp_path)
    [r] = search.search(conn, {"osnet": np.array([A])}, filters=search.Filters(bib="2001"))[:1]
    assert r.relpath == "1.jpg" and r.person_id == ids[1][1]
    assert ranked(conn, bib="2001") == ["1.jpg", "2.jpg"]
    assert ranked(conn, bib="200") == ["1.jpg", "2.jpg"]
    assert ranked(conn, bib="21") == ["3.jpg"]
    assert ranked(conn, bib="9999") == []


def test_filters_combine_with_and(tmp_path):
    _, conn, _ = filter_index(tmp_path)
    assert ranked(conn, bib="2001", albums=("9.25 赛事",), start="2026-09-25 09:00") == ["2.jpg"]
    assert ranked(conn, photographers=("阿光",), start="2026-09-25 07:00:00") == ["1.jpg"]
    assert ranked(conn, photographers=("Lens",), albums=("定妆照",)) == []


def test_bib_filter_without_ocr_names_stage(tmp_path):
    _, conn, _ = filter_index(tmp_path)
    conn.execute("update persons set ocr_at = null")
    with pytest.raises(search.MissingEmbeddings, match="ocr_bibs"):
        ranked(conn, bib="2001")
    assert ranked(conn, albums=("定妆照",)) == ["3.jpg"]


def cli_ranked(capsys, c, tmp_path, *flags):
    cli.main(["search", str(c), "--text", "red", *flags, "--out", str(tmp_path / "s.jpg")])
    return sorted(ln.split()[2] for ln in capsys.readouterr().out.splitlines() if ln.lstrip()[:1].isdigit())


@pytest.mark.parametrize("flags, expected", [
    ((), ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]),
    (("--from", "2026-09-25 09:00"), ["2.jpg", "3.jpg"]),
    (("--to", "2026-09-25 09:00"), ["1.jpg"]),
    (("--photographer", "Lens", "--photographer", "u3"), ["2.jpg", "3.jpg"]),
    (("--album", "定妆照"), ["3.jpg"]),
    (("--bib", "21"), ["3.jpg"]),
    (("--photographer", "u1", "--album", "9.25 赛事", "--from", "2026-09-25 07:00", "--to", "2026-09-25 09:00",
      "--bib", "001"), ["1.jpg"]),
])
def test_cli_each_filter_flag_restricts_results(tmp_path, monkeypatch, capsys, flags, expected):
    c, _, _ = filter_index(tmp_path)
    Fakes(monkeypatch, texts={"red": Y})
    assert cli_ranked(capsys, c, tmp_path, *flags) == expected


def test_cli_to_without_seconds_includes_the_whole_minute(tmp_path, monkeypatch, capsys):
    c, conn, _ = filter_index(tmp_path)
    conn.execute("update photos set taken_at = '2026-09-25 09:30:30' where relpath = '2.jpg'")
    conn.commit()
    Fakes(monkeypatch, texts={"red": Y})
    assert cli_ranked(capsys, c, tmp_path, "--to", "2026-09-25 09:30") == ["1.jpg", "2.jpg"]
    assert cli_ranked(capsys, c, tmp_path, "--to", "2026-09-25 09:30:29") == ["1.jpg"]
    assert cli_ranked(capsys, c, tmp_path, "--from", "2026-09-25 09:30") == ["2.jpg", "3.jpg"]
    assert cli_ranked(capsys, c, tmp_path, "--from", "2026-09-25 09:31") == ["3.jpg"]


@pytest.mark.parametrize("bib", ["", "   "])
def test_cli_rejects_empty_bib(tmp_path, monkeypatch, capsys, bib):
    c, _, _ = filter_index(tmp_path)
    fakes = Fakes(monkeypatch, texts={"red": Y})
    msg, _ = run(capsys, c, "--text", "red", "--bib", bib)
    assert "--bib" in msg and "\n" not in msg
    assert fakes.encoded == []


def test_cli_partial_ocr_warns_and_still_filters(tmp_path, monkeypatch, capsys):
    c, conn, ids = filter_index(tmp_path)
    conn.execute("update persons set ocr_at = null where id in (?, ?)", (ids[3][0], ids[4][0]))
    conn.commit()
    Fakes(monkeypatch, texts={"red": Y})
    cli.main(["search", str(c), "--text", "red", "--bib", "001", "--out", str(tmp_path / "s.jpg")])
    lines = capsys.readouterr().out.splitlines()
    assert "ocr_bibs incomplete: 2 of 5 persons not read yet; rerun `photofinder index`" in lines
    assert sorted(ln.split()[2] for ln in lines if ln.lstrip()[:1].isdigit()) == ["1.jpg", "2.jpg"]
    cli.main(["search", str(c), "--text", "red", "--out", str(tmp_path / "s.jpg")])
    assert "ocr_bibs incomplete" not in capsys.readouterr().out
    conn.execute("update persons set ocr_at = 'x'")
    conn.commit()
    cli.main(["search", str(c), "--text", "red", "--bib", "001", "--out", str(tmp_path / "s.jpg")])
    assert "ocr_bibs incomplete" not in capsys.readouterr().out


def test_cli_no_match_prints_line_and_writes_no_sheet(tmp_path, monkeypatch, capsys):
    c, _, _ = filter_index(tmp_path)
    Fakes(monkeypatch, texts={"red": Y})
    cli.main(["search", str(c), "--text", "red", "--bib", "9999", "--out", str(tmp_path / "s.jpg")])
    assert capsys.readouterr().out.strip() == "no photos match the filters"
    assert not (tmp_path / "s.jpg").exists()


@pytest.mark.parametrize("flag, value", [("--from", "2026-09-25"), ("--to", "25/09/2026 10:00"),
                                         ("--from", "2026-13-01 10:00")])
def test_cli_rejects_bad_time_with_one_line(tmp_path, monkeypatch, capsys, flag, value):
    c, _, _ = filter_index(tmp_path)
    fakes = Fakes(monkeypatch, texts={"red": Y})
    msg, _ = run(capsys, c, "--text", "red", flag, value)
    assert flag in msg and "YYYY-MM-DD HH:MM" in msg and "\n" not in msg
    assert fakes.encoded == []


def test_cli_filters_still_need_a_query_term(tmp_path, monkeypatch, capsys):
    c, _, _ = filter_index(tmp_path)
    Fakes(monkeypatch)
    msg, _ = run(capsys, c, "--bib", "2001")
    assert "--photo" in msg


def test_cli_bib_without_ocr_names_stage_before_models(tmp_path, monkeypatch, capsys):
    c, conn, _ = filter_index(tmp_path)
    conn.execute("update persons set ocr_at = null")
    conn.commit()
    fakes = Fakes(monkeypatch, texts={"red": Y})
    msg, _ = run(capsys, c, "--photo", query_photo(tmp_path), "--text", "red", "--bib", "2001")
    assert "ocr_bibs" in msg and "\n" not in msg
    assert fakes.detected == fakes.embedded == fakes.encoded == []
