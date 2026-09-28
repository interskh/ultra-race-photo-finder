import numpy as np
import pytest
from PIL import Image

from photofinder import cli, db, evaluate, models, search
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


def z(values):
    v = np.asarray(values, float)
    return (v - v.mean()) / v.std()


def fused(parts):
    return sum(w * z(c) for w, c in parts) / sum(w for w, _ in parts)


def test_nearest_person_ranks_its_photo_first(tmp_path):
    o, s = [A, [0.1, 1, 0, 0], [0.5, 0.5, 0, 0]], [X, [0.1, 1, 0], [0.5, 0.5, 0]]
    _, conn, _ = make_index(tmp_path, [
        (1, (0, 0, 50, 100), o[0], s[0]),
        (2, (10, 10, 60, 110), o[1], s[1]),
        (3, (0, 0, 40, 90), o[2], s[2]),
    ])
    results = search.search(conn, {"osnet": np.array([B]), "siglip": np.array([Y])})
    assert [r.relpath for r in results] == ["2.jpg", "3.jpg", "1.jpg"]
    assert [r.rank for r in results] == [1, 2, 3]
    assert results[0].box == (10, 10, 60, 110)
    want = fused([(search.WEIGHTS["osnet"], [cos(v, B) for v in o]), (search.WEIGHTS["siglip"], [cos(v, Y) for v in s])])
    assert [r.score for r in results] == pytest.approx(want[[1, 2, 0]], abs=5e-3)


def test_combined_terms_are_zscored_so_small_cosine_terms_still_move_the_ranking(tmp_path):
    _, conn, _ = make_index(tmp_path, [
        (1, (0, 0, 50, 100), [1, 0.10, 0, 0], X),
        (2, (0, 0, 50, 100), [1, 0.3, 0, 0], X),
        (3, (0, 0, 50, 100), [1, 0.6, 0, 0], X),
    ])
    add_scenes(conn, [(1, [0.02, 1, 0]), (2, [0.06, 1, 0]), (3, [0.0, 1, 0])])
    refs = {"osnet": np.array([A]), "scene": np.array([X])}
    assert [r.relpath for r in search.search(conn, {"osnet": np.array([A])})] == ["1.jpg", "2.jpg", "3.jpg"]
    results = search.search(conn, refs, weights={"osnet": 1.0, "scene": 0.5})
    assert [r.relpath for r in results] == ["2.jpg", "1.jpg", "3.jpg"]
    o = [cos(v, A) for v in ([1, 0.1, 0, 0], [1, 0.3, 0, 0], [1, 0.6, 0, 0])]
    sc = [cos(v, X) for v in ([0.02, 1, 0], [0.06, 1, 0], [0, 1, 0])]
    assert [r.score for r in results] == pytest.approx(fused([(1.0, o), (0.5, sc)])[[1, 0, 2]], abs=5e-3)


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


def test_negatives_subtract_normalized_not_me_similarity_after_fusion(tmp_path):
    o = [A, [0.8, 0.6, 0, 0], [0.8, 0, 0.6, 0], B]
    s = [X, [0.8, 0.6, 0], [0.8, 0.6, 0], Y]
    _, conn, _ = make_index(tmp_path, [(i + 1, (0, 0, 9, 9), o[i], s[i]) for i in range(4)], photos=4)
    persons = search.load_persons(conn)
    refs = {"osnet": np.array([A]), "siglip": np.array([X])}
    base = search.score(persons, refs)
    assert search.score(persons, refs, negatives=np.empty((0, 4))).tolist() == base.tolist()
    got = search.score(persons, refs, negatives=np.array([B]))
    w = search.WEIGHTS
    neg = [cos(v, B) for v in o]
    want = fused([(w["osnet"], [cos(v, A) for v in o]), (w["siglip"], [cos(v, X) for v in s])]) \
        - search.NEG_WEIGHT / (w["osnet"] + w["siglip"]) * z(neg)
    assert got == pytest.approx(want, abs=5e-3)
    assert base[1] == pytest.approx(base[2], abs=1e-3) and got[1] < got[2] - 0.1
    got = search.score(persons, {"osnet": np.array([A])}, weights={"osnet": 2.0}, negatives=np.array([B]))
    want = np.array([cos(v, A) for v in o]) - search.NEG_WEIGHT / 2.0 * np.array(neg)
    assert got == pytest.approx(want, abs=5e-3)


def test_max_cos_renormalizes_float16_chunks(monkeypatch):
    monkeypatch.setattr(search, "CHUNK", 2)
    vecs = np.array([[2, 0, 0, 0], [0, 3, 0, 0], [1, 1, 0, 0]], np.float16)
    assert search.max_cos(vecs, np.array([A, B])) == pytest.approx([1, 1, 2 ** -0.5], abs=2e-3)


def test_score_takes_max_over_refs(tmp_path):
    _, conn, ids = make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (2, (0, 0, 50, 100), C, Z),
        (3, (0, 0, 50, 100), [0.2, 0.2, 1, 0], [0.2, 0.2, 1]),
    ])
    persons = search.load_persons(conn)
    refs = search.person_refs(persons, [ids[1][0], ids[2][0]])
    scores = dict(zip(persons.ids, search.score(persons, {"siglip": refs["siglip"]})))
    assert scores[ids[1][0]] == pytest.approx(1, abs=2e-3)
    assert scores[ids[2][0]] == pytest.approx(1, abs=2e-3)
    assert scores[ids[3][0]] == pytest.approx(cos([0.2, 0.2, 1], Z), abs=2e-3)


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
    assert [r.relpath for r in results] == ["3.jpg", "1.jpg", "2.jpg", "4.jpg"]
    assert [round(r.score, 3) for r in results] == pytest.approx(
        [cos([1, 0.1, 0], X), cos([0.5, 1, 0], X), cos([-0.3, 1, 0], X), cos([-0.3, 1, 0], X) - 1], abs=2e-3)
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
    assert [r.relpath for r in search.search(conn, {"osnet": np.array([A]), "text": np.array([Y])},
                                             weights={"osnet": 1.0, "text": 0.5})] == ["1.jpg", "2.jpg"]


@pytest.mark.parametrize("weights", [search.WEIGHTS, {"osnet": 1.0, "siglip": 0.0, "text": 2.0, "scene": 3.0}])
def test_photo_text_and_scene_terms_renormalize(tmp_path, weights):
    o = [[0.6, 0.8, 0.3, 0.1], [0.1, 0.2, 0.9, 0.3], [0.5, 0.1, 0.1, 0.8]]
    s = [[0.2, 0.9, 0.4], [0.9, 0.1, 0.2], [0.3, 0.3, 0.8]]
    sc = [[0.7, 0.1, 0.7], [0.1, 0.9, 0.2], [0.8, 0.5, 0.1]]
    _, conn, ids = make_index(tmp_path, [(i + 1, (0, 0, 50, 100), o[i], s[i]) for i in range(3)])
    add_scenes(conn, [(i + 1, sc[i]) for i in range(3)])
    qo, qt, qs = [2.0, 1.0, 0.0, 0.5], [0.1, 1.0, 0.3], [1.0, 0.2, 0.0]
    results = search.search(conn, {"osnet": np.array([qo]), "text": np.array([qt]), "scene": np.array([qs])},
                            weights=weights)
    want = fused([(weights["osnet"], [cos(v, qo) for v in o]), (weights["text"], [cos(v, qt) for v in s]),
                  (weights["scene"], [cos(v, qs) for v in sc])])
    assert {r.person_id: r.score for r in results} == pytest.approx(
        {ids[i + 1][0]: want[i] for i in range(3)}, abs=5e-3)


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
    lines = capsys.readouterr().out.splitlines()
    assert "embed_scenes incomplete: 1 of 5 photos have no scene vector yet; rerun `photofinder index`" in lines
    ranked = [ln.split() for ln in lines if ln.lstrip()[:1].isdigit()]
    assert [r[2] for r in ranked] == ["3.jpg", "1.jpg", "2.jpg", "4.jpg"]
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


def capture_sheets(monkeypatch):
    sheets = []
    monkeypatch.setattr(search, "contact_sheet", lambda tiles, out, **kw: sheets.append((tiles, out)))
    return sheets


def test_cli_scene_only_sheet_shows_whole_photos(tmp_path, monkeypatch, capsys):
    c, _, _ = scene_index(tmp_path)
    Fakes(monkeypatch, texts={"雪山": X})
    sheets = capture_sheets(monkeypatch)
    cli.main(["search", str(c), "--scene", "雪山", "--out", str(tmp_path / "s.jpg")])
    cli.main(["search", str(c), "--scene", "雪山", "--text", "雪山", "--out", str(tmp_path / "s.jpg")])
    (whole, _), (crops, _) = sheets
    assert [img.size for img, _ in whole] == [(200, 300)] * 4
    assert all(img.size == (50, 100) for img, _ in crops)


def eval_index(tmp_path):
    c, conn, ids = make_index(tmp_path, [
        (1, (0, 0, 100, 200), A, X),
        (1, (100, 0, 110, 20), A, X),
        (1, (0, 0, 190, 290), C, Z),
        (2, (0, 0, 50, 100), [1, 0.1, 0, 0], [1, 0.1, 0]),
        (2, (60, 0, 110, 100), [1, 0.05, 0, 0], [1, 0.1, 0]),
        (3, (0, 0, 50, 100), [1, 0.3, 0, 0], [1, 0.3, 0]),
        (4, (0, 0, 50, 100), [1, 0.2, 0, 0], [1, 0.2, 0]),
        (5, (0, 0, 50, 100), B, [1, 0.05, 0]),
        (6, (0, 0, 50, 100), C, Z),
    ], photos=6)
    conn.executemany("update photos set photographer_uid = ? where relpath = ?",
                     [(u, f"{i}.jpg") for i, u in enumerate(["u1", "u1", "u2", "u3", "u2", "u3"], 1)])
    conn.executemany("insert into bibs(person_id, text, conf) values (?,?,?)", [
        (ids[1][0], "2001", 1.0), (ids[1][1], "2001", 0.5), (ids[2][0], "2001", 1.0), (ids[3][0], "2001", 1.0),
        (ids[4][0], "12001", 1.0), (ids[5][0], "200", 1.0),
        *[(ids[p][-1], "157", 0.5) for p in (3, 4, 5, 6)]])
    conn.execute("update persons set ocr_at = '2026-09-28 00:00:00'")
    conn.commit()
    photo = dict(conn.execute("select cast(replace(relpath, '.jpg', '') as integer), id from photos"))
    return c, conn, ids, photo


def test_ground_truth_is_exact_bib_match_with_largest_box_refs(tmp_path):
    _, conn, ids, photo = eval_index(tmp_path)
    persons = search.load_persons(conn)
    truth = evaluate.ground_truth(conn, "2001", persons)
    assert set(truth.photos) == {photo[1], photo[2], photo[3]}
    assert truth.refs == [(ids[1][0], photo[1]), (ids[2][0], photo[2]), (ids[3][0], photo[3])]
    assert truth.photos[photo[3]] == "u2"
    assert set(evaluate.ground_truth(conn, "200", persons).photos) == {photo[5]}
    assert evaluate.ground_truth(conn, "2001", persons, cap=2).refs == truth.refs[:2]


def test_recall_arithmetic_on_hand_built_ranking():
    assert evaluate.recall([5, 3, 9, 1], {1, 3, 7}, 2) == pytest.approx(1 / 3)
    assert evaluate.recall([5, 3, 9, 1], {1, 3, 7}, 4) == pytest.approx(2 / 3)
    assert evaluate.recall([5, 3], set(), 2) is None


def test_evaluate_ref_excludes_ref_photo_and_splits_cross_photographer(tmp_path, monkeypatch):
    _, conn, ids, photo = eval_index(tmp_path)
    persons = search.load_persons(conn)
    truth = evaluate.ground_truth(conn, "2001", persons)
    picked, _ = evaluate.rank_photos(persons, ids[1][0], photo[1], {"osnet": 1.0})
    assert [int(persons.photo_ids[i]) for i in picked] == [photo[p] for p in (2, 4, 3, 5, 6)]
    monkeypatch.setattr(evaluate, "KS", (1, 2))
    assert evaluate.evaluate_ref(persons, truth, ids[1][0], photo[1], {"osnet": 1.0})[:3] == (0.5, 0.5, 0.0)
    monkeypatch.setattr(evaluate, "KS", (1, 3))
    assert evaluate.evaluate_ref(persons, truth, ids[1][0], photo[1], {"osnet": 1.0})[:3] == (0.5, 1.0, 1.0)
    lone = evaluate.Truth("x", {photo[1]: "u1"}, [(ids[1][0], photo[1])])
    assert evaluate.evaluate_ref(persons, lone, ids[1][0], photo[1], {"osnet": 1.0}) is None


def test_strict_hit_needs_the_matched_person_to_carry_the_bib(tmp_path, monkeypatch):
    _, conn, ids, photo = eval_index(tmp_path)
    persons = search.load_persons(conn)
    truth = evaluate.ground_truth(conn, "2001", persons)
    assert truth.persons == {ids[1][0], ids[1][1], ids[2][0], ids[3][0]}
    picked, _ = evaluate.rank_photos(persons, ids[1][0], photo[1], {"osnet": 1.0})
    assert int(persons.ids[picked[0]]) == ids[2][1]
    monkeypatch.setattr(evaluate, "KS", (1, 2))
    assert evaluate.evaluate_ref(persons, truth, ids[1][0], photo[1], {"osnet": 1.0})[3:] == (0.0, 0.0)
    monkeypatch.setattr(evaluate, "KS", (1, 3))
    assert evaluate.evaluate_ref(persons, truth, ids[1][0], photo[1], {"osnet": 1.0})[3:] == (0.0, 0.5)
    [row] = evaluate.evaluate_bib(persons, truth, {"o": {"osnet": 1.0}})
    assert (row.r50, row.s50) == (1.0, pytest.approx((0.5 + 1 + 1) / 3))


def test_weight_configs_change_the_ranking(tmp_path):
    _, conn, ids, photo = eval_index(tmp_path)
    persons = search.load_persons(conn)
    order = {name: [int(persons.photo_ids[i]) for i in evaluate.rank_photos(persons, ids[1][0], photo[1], w, 3)[0]]
             for name, w in evaluate.configs().items()}
    assert order["osnet"] == [photo[2], photo[4], photo[3]]
    assert order["siglip"] == [photo[5], photo[2], photo[4]]
    assert list(evaluate.configs()) == ["osnet", "siglip", "0.3:0.7", "0.5:0.5", "0.7:0.3"]
    assert "default 0.6:0.4" in evaluate.configs({"osnet": 0.6, "siglip": 0.4})


def test_mean_over_bibs_weights_each_bib_equally():
    rows = [[evaluate.Row("a", 0.2, 0.4, float("nan"), 0.1, 0.3, 1, 0, 3)],
            [evaluate.Row("a", 0.6, 0.8, 0.5, 0.3, 0.5, 9, 4, 12)]]
    [m] = evaluate.mean_over_bibs(rows)
    assert (m.r10, m.r50, m.x50, m.s10, m.s50, m.refs, m.xrefs, m.photos) == (
        pytest.approx(0.4), pytest.approx(0.6), 0.5, pytest.approx(0.2), pytest.approx(0.4), 10, 4, 15)


def test_frequent_bibs_lists_four_digit_first(tmp_path):
    _, conn, _, _ = eval_index(tmp_path)
    assert evaluate.frequent_bibs(conn) == [("2001", 3, 2), ("157", 4, 2), ("12001", 1, 1), ("200", 1, 1)]


def no_models(monkeypatch, tmp_path):
    def boom(name, load):
        raise AssertionError(f"eval loaded model {name}")
    monkeypatch.setattr(models, "_get", boom)
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    monkeypatch.setattr(cli.config, "DATA_ROOT", tmp_path / "data")


def test_cli_eval_reports_recall_and_writes_sheet_without_models(tmp_path, monkeypatch, capsys):
    c, _, _, _ = eval_index(tmp_path)
    no_models(monkeypatch, tmp_path)
    cli.main(["eval", str(c), "--bib", "2001", "--bib", "157"])
    out = capsys.readouterr().out
    assert "bib 2001" in out and "bib 157" in out and "mean over 2 bibs" in out
    rows = [ln.split() for ln in out.splitlines() if ln.split()[:1] == ["0.3:0.7"]]
    assert len(rows) == 3 and rows[0][-3:] == ["3", "3", "3"]
    sheets = sorted((tmp_path / "data" / "exports").glob("*.jpg"))
    assert [s.name.split("-")[:3] for s in sheets] == [["coll", "eval", "157"], ["coll", "eval", "2001"]]
    assert "most frequent" not in out


def test_eval_sheet_marks_gt_hits_and_shows_ocr_text(tmp_path, monkeypatch):
    c, conn, ids, photo = eval_index(tmp_path)
    persons = search.load_persons(conn)
    sheets = capture_sheets(monkeypatch)
    evaluate.sheet(conn, c, persons, evaluate.ground_truth(conn, "2001", persons), {"osnet": 1.0}, tmp_path / "s.jpg")
    [(tiles, _)] = sheets
    labels = [t for _, t in tiles]
    assert labels[0].startswith("query\nbib 2001\n")
    assert tiles[0][0].size == (100, 200)
    assert [lb.split("\n")[1] for lb in labels[1:]] == [f"id {photo[p]}" for p in (2, 4, 3, 5, 6)]
    assert [lb.split("\n")[0].split()[2:] for lb in labels[1:]] == [["bib-photo"], [], ["BIB"], [], []]
    assert "ocr" not in labels[1] and "ocr 2001" in labels[3]
    assert "ocr 12001" in labels[2] and "ocr 200/157" in labels[4]
    assert tiles[1][0].size != (50, 100) and tiles[2][0].size == (50, 100)
    assert tiles[1][0].getpixel((0, 0)) != tiles[3][0].getpixel((0, 0))


@pytest.mark.parametrize("flags, says", [((), "no --bib given"), (("--bib", "200"), "bib 200: 1 photo(s)"),
                                          (("--bib", "9999"), "bib 9999: 0 photo(s)")])
def test_cli_eval_lists_frequent_bibs_when_bib_unusable(tmp_path, monkeypatch, capsys, flags, says):
    c, _, _, _ = eval_index(tmp_path)
    no_models(monkeypatch, tmp_path)
    cli.main(["eval", str(c), *flags])
    lines = capsys.readouterr().out.splitlines()
    assert says in "\n".join(lines)
    i = lines.index("most frequent OCR bibs (4-digit first): text photos photographers")
    assert lines[i + 1].split() == ["2001", "3", "2"]
    assert not (tmp_path / "data" / "exports").exists()


def test_cli_eval_without_ocr_names_stage(tmp_path, monkeypatch, capsys):
    c, conn, _, _ = eval_index(tmp_path)
    conn.execute("update persons set ocr_at = null")
    conn.commit()
    no_models(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as e:
        cli.main(["eval", str(c), "--bib", "2001"])
    assert "ocr_bibs" in str(e.value.code) and "\n" not in str(e.value.code)


def test_cli_eval_without_embeddings_names_stage(tmp_path, monkeypatch, capsys):
    c, _, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X)], embed=False)
    no_models(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as e:
        cli.main(["eval", str(c), "--bib", "2001"])
    assert "embed_persons" in str(e.value.code)


def test_cli_eval_reads_persons_and_ground_truth_from_one_snapshot(tmp_path, monkeypatch, capsys):
    c, _, ids, _ = eval_index(tmp_path)
    no_models(monkeypatch, tmp_path)
    real = search.load_persons

    def load_then_concurrent_write(conn):
        persons = real(conn)
        other = db.connect(c)
        other.executemany("insert into bibs(person_id, text, conf) values (?, '4242', 1.0)",
                          [(ids[4][0],), (ids[5][0],)])
        other.commit()
        other.close()
        return persons
    monkeypatch.setattr(search, "load_persons", load_then_concurrent_write)
    cli.main(["eval", str(c), "--bib", "4242"])
    assert "bib 4242: 0 photo(s)" in capsys.readouterr().out


def test_check_scenes_counts_ok_photos_without_scene_vector(tmp_path):
    _, conn, _ = scene_index(tmp_path)
    assert search.check_scenes(conn) == ("embed_scenes incomplete: 1 of 5 photos have no scene vector yet; "
                                         "rerun `photofinder index`")
    add_scenes(conn, [(4, Y)])
    assert search.check_scenes(conn) is None


def test_missing_scene_vector_ranks_last_in_combined_query(tmp_path):
    _, conn, _ = scene_index(tmp_path)
    results = search.search(conn, {"osnet": np.array([A]), "scene": np.array([X])},
                            weights={"osnet": 1.0, "scene": 1.0})
    assert results[-1].relpath == "2.jpg" and results[0].relpath == "1.jpg"
    assert [r.relpath for r in results].index("4.jpg") < 3
    only_scene = search.search(conn, {"scene": np.array([[-0.3, 1, 0]])})
    assert only_scene[-1].relpath == "4.jpg"


def test_nan_embedding_ranks_last_and_does_not_blank_its_term(tmp_path):
    nan = [float("nan")] * 4
    _, conn, _ = make_index(tmp_path, [
        (1, (0, 0, 50, 100), nan, X),
        (2, (0, 0, 50, 100), B, X),
        (3, (0, 0, 50, 100), A, X),
    ])
    refs = {"osnet": np.array([A]), "siglip": np.array([X])}
    assert [r.relpath for r in search.search(conn, refs)] == ["3.jpg", "2.jpg", "1.jpg"]
    results = search.search(conn, {"osnet": np.array([A])})
    assert [r.relpath for r in results] == ["3.jpg", "2.jpg", "1.jpg"]
    assert np.isfinite(results[-1].score) and results[-1].score < results[-2].score


def test_scene_term_still_moves_combined_ranking_on_mostly_missing_scenes(tmp_path):
    far = [0, 0, 1.0, 0]
    _, conn, _ = make_index(tmp_path, [
        (1, (0, 0, 50, 100), [1, 0.1, 0, 0], X),
        (2, (0, 0, 50, 100), [1, 0.3, 0, 0], X),
        (3, (0, 0, 50, 100), [1, 0.6, 0, 0], X),
        (4, (0, 0, 50, 100), [1, 0.9, 0, 0], X),
        *[(p, (0, 0, 50, 100), far, X) for p in range(5, 11)],
    ], photos=10)
    add_scenes(conn, [(1, [0.02, 1, 0]), (2, [0.06, 1, 0]), (3, [0.0, 1, 0]), (4, [0.03, 1, 0])])
    results = search.search(conn, {"osnet": np.array([A]), "scene": np.array([X])},
                            weights={"osnet": 1.0, "scene": 0.5})
    assert [r.relpath for r in results[:2]] == ["2.jpg", "1.jpg"]
    assert {r.relpath for r in results[-6:]} == {f"{p}.jpg" for p in range(5, 11)}


@pytest.mark.parametrize("flag", [("--box", "1"), ("--whole",)])
def test_cli_box_or_whole_without_photo_exits(tmp_path, monkeypatch, capsys, flag):
    c, _, _ = search_index(tmp_path)
    fakes = Fakes(monkeypatch, texts={"red": Y})
    msg, _ = run(capsys, c, "--text", "red", *flag)
    assert flag[0] in msg and "--photo" in msg and "\n" not in msg
    assert fakes.encoded == []


def test_all_invalid_negatives_leave_scores_unchanged(tmp_path):
    _, conn, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X), (2, (0, 0, 9, 9), B, Y)])
    persons = search.load_persons(conn)
    refs = {"text": np.array([X])}
    base = search.score(persons, refs)
    got = search.score(persons, refs, negatives=np.array([[float("nan")] * 4]))
    assert got.tolist() == base.tolist()
