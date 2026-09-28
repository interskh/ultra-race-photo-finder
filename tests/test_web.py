import csv
import io
import re
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from photofinder import cli, config, db, models, search
from photofinder.web import app as web
from test_search import A, B, C, X, Y, Z, add_scenes, filter_index, make_index


@pytest.fixture(autouse=True)
def no_real_models(monkeypatch):
    def boom(name, load):
        raise AssertionError(f"model loader called: {name}")
    monkeypatch.setattr(models, "_get", boom)


class FakeModels:
    def __init__(self, monkeypatch, boxes=(), osnet=(), siglip=(), texts=None):
        self.boxes, self.osnet, self.siglip, self.texts = list(boxes), list(osnet), list(siglip), texts or {}
        self.detected, self.embedded, self.encoded = [], [], []
        monkeypatch.setattr(models, "detect_persons", self.detect)
        monkeypatch.setattr(models, "embed_crops", self.embed)
        monkeypatch.setattr(models, "encode_text", self.encode)

    def detect(self, images):
        self.detected.append([img.size for img in images])
        return [list(self.boxes) for _ in images]

    def embed(self, crops):
        self.embedded.append([c.size for c in crops])
        return models.l2norm(self.osnet[:len(crops)]), models.l2norm(self.siglip[:len(crops)])

    def encode(self, texts):
        self.encoded.append(list(texts))
        return models.l2norm([self.texts[t] for t in texts])


def client(c):
    return TestClient(web.create_app(c))


def photo_ids(conn):
    return {rel: i for i, rel in conn.execute("select id, relpath from photos")}


def ranked_photos(res):
    assert res.status_code == 200, res.text
    return [r["relpath"] for r in res.json()["results"]]


def jpeg_bytes(size=(160, 300)):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "JPEG")
    return buf.getvalue()


def bib_index(tmp_path):
    c, conn, ids = filter_index(tmp_path)
    conn.executemany("insert into bibs(person_id, text, conf) values (?,?,?)",
                     [(ids[3][0], "2001", 0.9), (ids[4][0], "2001", 0.8)])
    conn.commit()
    return c, conn, ids


def test_bib_start_is_exact_one_per_photo_ordered_by_time_null_last(tmp_path):
    c, _, ids = bib_index(tmp_path)
    api = client(c)
    res = api.post("/api/search", json={"start_bib": "2001"})
    assert ranked_photos(res) == ["1.jpg", "3.jpg", "4.jpg"]
    first = res.json()["results"][0]
    assert first["person_id"] == ids[1][1] and first["box"] == [60, 0, 110, 100]
    assert first["bibs"] == [{"text": "2001", "conf": 1.0}] and first["score"] is None
    assert res.json()["total"] == 3
    assert ranked_photos(api.post("/api/search", json={"start_bib": "001"})) == []
    assert ranked_photos(api.post("/api/search", json={"start_bib": "12001"})) == ["2.jpg"]
    assert ranked_photos(api.post("/api/search", json={"start_bib": "2001", "albums": ["定妆照"]})) == ["3.jpg"]
    assert ranked_photos(api.post("/api/search", json={"start_bib": "2001", "start": "2026-09-25 09:00"})) == ["3.jpg"]
    page = api.post("/api/search", json={"start_bib": "2001", "top": 1, "offset": 1}).json()["results"]
    assert [(r["rank"], r["relpath"]) for r in page] == [(2, "3.jpg")]


def test_bib_start_rejects_empty_and_combination(tmp_path):
    c, _, ids = bib_index(tmp_path)
    api = client(c)
    assert api.post("/api/search", json={"start_bib": " "}).status_code == 400
    res = api.post("/api/search", json={"start_bib": "2001", "persons": [ids[1][0]]})
    assert res.status_code == 400 and "start_bib" in res.json()["detail"]


def test_person_ref_search_ranks_photos_without_loading_models(tmp_path):
    c, conn, ids = make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (2, (10, 20, 110, 220), [0.9, 0.1, 0, 0], [0.9, 0.1, 0]),
        (3, (0, 0, 40, 90), [0.5, 0.5, 0, 0], [0.5, 0.5, 0]),
        (3, (50, 0, 90, 90), C, Z),
    ])
    res = client(c).post("/api/search", json={"persons": [ids[1][0]]})
    assert ranked_photos(res) == ["1.jpg", "2.jpg", "3.jpg"]
    body = res.json()
    assert [r["rank"] for r in body["results"]] == [1, 2, 3]
    assert body["results"][1]["box"] == [10, 20, 110, 220] and body["results"][2]["person_id"] == ids[3][0]
    assert body["results"][0]["score"] > body["results"][1]["score"] > body["results"][2]["score"]
    assert body["results"][1]["width"] == 200 and body["results"][1]["height"] == 300
    assert body["warnings"] == [] and body["timing"] >= 0


def test_unknown_person_and_empty_query_are_400(tmp_path):
    c, _, ids = make_index(tmp_path, [(1, (0, 0, 50, 100), A, X)])
    api = client(c)
    res = api.post("/api/search", json={"persons": [999]})
    assert res.status_code == 400 and "999" in res.json()["detail"]
    assert api.post("/api/search", json={}).status_code == 400


def upload_index(tmp_path):
    return make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (2, (0, 0, 50, 100), B, Y),
        (3, (0, 0, 50, 100), C, Z),
    ])


def test_upload_returns_boxes_by_area_and_search_uses_stored_vectors(tmp_path, monkeypatch):
    c, _, _ = upload_index(tmp_path)
    fakes = FakeModels(monkeypatch, boxes=[(10.0, 10.0, 40.0, 60.0, 0.9), (20.0, 30.0, 140.0, 280.0, 0.8)],
                       osnet=[C, A, B], siglip=[Z, X, Y])
    api = client(c)
    res = api.post("/api/upload", files={"file": ("q.jpg", jpeg_bytes(), "image/jpeg")})
    assert res.status_code == 200, res.text
    up = res.json()
    assert [b["box"] for b in up["boxes"]] == [[20, 30, 140, 280], [10, 10, 40, 60]]
    assert (up["width"], up["height"]) == (160, 300)
    assert fakes.detected == [[(160, 300)]]
    assert fakes.embedded == [[(120, 250), (30, 50), (160, 300)]]
    top = lambda box: ranked_photos(api.post("/api/search", json={"upload": up["token"], "box": box}))[0]
    assert top(0) == "3.jpg"
    assert top(1) == "1.jpg"
    assert top("whole") == "2.jpg"
    assert len(fakes.embedded) == 1 and len(fakes.detected) == 1
    assert api.post("/api/search", json={"upload": up["token"], "box": 2}).status_code == 400
    img = api.get(f"/api/uploads/{up['token']}/image")
    assert img.headers["content-type"] == "image/jpeg" and "max-age" in img.headers["cache-control"]
    assert Image.open(io.BytesIO(img.content)).size == (160, 300)


def test_upload_with_no_person_offers_whole_image(tmp_path, monkeypatch):
    c, _, _ = upload_index(tmp_path)
    fakes = FakeModels(monkeypatch, boxes=[], osnet=[B], siglip=[Y])
    api = client(c)
    up = api.post("/api/upload", files={"file": ("q.jpg", jpeg_bytes(), "image/jpeg")}).json()
    assert up["boxes"] == []
    assert fakes.embedded == [[(160, 300)]]
    assert ranked_photos(api.post("/api/search", json={"upload": up["token"], "box": "whole"}))[0] == "2.jpg"
    res = api.post("/api/search", json={"upload": up["token"], "box": 0})
    assert res.status_code == 400 and "whole" in res.json()["detail"]


def test_upload_rejects_non_image_and_unknown_token(tmp_path, monkeypatch):
    c, _, _ = upload_index(tmp_path)
    fakes = FakeModels(monkeypatch)
    api = client(c)
    res = api.post("/api/upload", files={"file": ("q.txt", b"hello", "text/plain")})
    assert res.status_code == 400 and "image" in res.json()["detail"]
    assert fakes.detected == [] and fakes.embedded == []
    assert api.post("/api/search", json={"upload": "nope"}).status_code == 404
    assert api.get("/api/uploads/nope/image").status_code == 404


def test_upload_cache_keeps_the_last_uploads_only(tmp_path, monkeypatch):
    c, _, _ = upload_index(tmp_path)
    FakeModels(monkeypatch, osnet=[B], siglip=[Y])
    monkeypatch.setattr(web, "UPLOADS", 2)
    api = client(c)
    tokens = [api.post("/api/upload", files={"file": ("q.jpg", jpeg_bytes(), "image/jpeg")}).json()["token"]
              for _ in range(3)]
    assert api.get(f"/api/uploads/{tokens[0]}/image").status_code == 404
    assert all(api.get(f"/api/uploads/{t}/image").status_code == 200 for t in tokens[1:])


def label(api, person_id, value):
    res = api.post("/api/labels", json={"person_id": person_id, "label": value})
    assert res.status_code == 200, res.text
    return res


def test_labels_set_clear_persist_and_show_on_results_and_detail(tmp_path):
    c, conn, ids = make_index(tmp_path, [(1, (0, 0, 50, 100), A, X), (1, (60, 0, 110, 100), B, Y),
                                         (2, (0, 0, 50, 100), C, Z)])
    api = client(c)
    label(api, ids[1][0], "me")
    label(api, ids[1][1], "not_me")
    label(api, ids[2][0], "me")
    label(api, ids[2][0], None)
    assert api.post("/api/labels", json={"person_id": 999, "label": "me"}).status_code == 404
    assert api.post("/api/labels", json={"person_id": ids[1][0], "label": "maybe"}).status_code == 400
    assert dict(conn.execute("select person_id, label from labels")) == {ids[1][0]: "me", ids[1][1]: "not_me"}
    again = client(c)
    detail = again.get(f"/api/photos/{photo_ids(conn)['1.jpg']}").json()
    assert [(p["person_id"], p["label"]) for p in detail["persons"]] == [(ids[1][0], "me"), (ids[1][1], "not_me")]
    results = again.post("/api/search", json={"persons": [ids[1][1]]}).json()["results"]
    assert {r["person_id"]: r["label"] for r in results} == {ids[1][1]: "not_me", ids[2][0]: None}
    label(again, ids[1][0], "not_me")
    assert dict(conn.execute("select person_id, label from labels")) == {ids[1][0]: "not_me", ids[1][1]: "not_me"}


def more_index(tmp_path):
    return make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (1, (60, 0, 110, 100), [0.9, 0.1, 0, 0], X),
        (2, (0, 0, 50, 100), [0.95, 0.05, 0, 0], X),
        (2, (60, 0, 110, 100), [0.5, 0.5, 0, 0], [0.5, 0.5, 0]),
        (3, (0, 0, 50, 100), [0.7, 0.3, 0, 0], [0.7, 0.3, 0]),
        (4, (0, 0, 50, 100), C, Z),
    ], photos=4)


def test_find_more_uses_all_me_refs_and_hides_labelled(tmp_path):
    c, _, ids = more_index(tmp_path)
    api = client(c)
    res = api.post("/api/search", json={"mode": "more"})
    assert res.status_code == 400 and "me" in res.json()["detail"]
    label(api, ids[1][0], "me")
    label(api, ids[2][0], "not_me")
    results = api.post("/api/search", json={"mode": "more"}).json()["results"]
    assert [(r["relpath"], r["person_id"]) for r in results] == [("3.jpg", ids[3][0]), ("2.jpg", ids[2][1]),
                                                                ("4.jpg", ids[4][0])]
    similar = api.post("/api/search", json={"persons": [ids[1][0]]}).json()["results"]
    assert [(r["relpath"], r["person_id"]) for r in similar][:2] == [("1.jpg", ids[1][0]), ("2.jpg", ids[2][0])]
    label(api, ids[4][0], "me")
    results = api.post("/api/search", json={"mode": "more", "top": 1}).json()["results"]
    assert [(r["relpath"], r["person_id"]) for r in results] == [("3.jpg", ids[3][0])]
    label(api, ids[1][0], None)
    results = api.post("/api/search", json={"mode": "more"}).json()["results"]
    assert "1.jpg" in [r["relpath"] for r in results] and "4.jpg" not in [r["relpath"] for r in results]


def test_find_more_ref_set_is_every_me_person(tmp_path):
    near_c, near_a, mid_a = [0.1, 0, 0.99, 0], [0.99, 0.1, 0, 0], [0.6, 0.8, 0, 0]
    c, _, ids = make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (2, (0, 0, 50, 100), C, Z),
        (3, (0, 0, 50, 100), near_c, near_c[:3]),
        (4, (0, 0, 50, 100), near_a, near_a[:3]),
        (5, (0, 0, 50, 100), mid_a, mid_a[:3]),
    ], photos=5)
    api = client(c)
    label(api, ids[1][0], "me")
    assert ranked_photos(api.post("/api/search", json={"mode": "more"})) == ["4.jpg", "5.jpg", "3.jpg", "2.jpg"]
    label(api, ids[2][0], "me")
    assert sorted(ranked_photos(api.post("/api/search", json={"mode": "more"}))[:2]) == ["3.jpg", "4.jpg"]


def test_not_me_negative_demotes_its_near_duplicate(tmp_path):
    c, _, ids = make_index(tmp_path, [
        (1, (0, 0, 50, 100), A, X),
        (2, (0, 0, 50, 100), [0.8, 0.6, 0, 0], [0.8, 0.6, 0]),
        (3, (0, 0, 50, 100), [0.8, 0, 0.6, 0], [0.8, 0.6, 0]),
        (4, (0, 0, 50, 100), B, Y),
    ], photos=4)
    api = client(c)
    label(api, ids[1][0], "me")
    assert ranked_photos(api.post("/api/search", json={"mode": "more"})) == ["2.jpg", "3.jpg", "4.jpg"]
    label(api, ids[4][0], "not_me")
    assert ranked_photos(api.post("/api/search", json={"mode": "more"})) == ["3.jpg", "2.jpg"]
    assert ranked_photos(api.post("/api/search", json={"persons": [ids[1][0]]}))[1:3] == ["3.jpg", "2.jpg"]


@pytest.mark.parametrize("extra, expected", [
    ({}, ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]),
    ({"start": "2026-09-25 09:00"}, ["2.jpg", "3.jpg"]),
    ({"end": "2026-09-25 09:30"}, ["1.jpg", "2.jpg"]),
    ({"end": "2026-09-25 09:29"}, ["1.jpg"]),
    ({"photographers": ["Lens", "u3"]}, ["2.jpg", "3.jpg"]),
    ({"albums": ["定妆照"]}, ["3.jpg"]),
    ({"bib": "200"}, ["1.jpg", "2.jpg"]),
    ({"bib": "  "}, ["1.jpg", "2.jpg", "3.jpg", "4.jpg"]),
])
def test_filters_restrict_search(tmp_path, extra, expected):
    c, _, ids = filter_index(tmp_path)
    res = client(c).post("/api/search", json={"persons": [ids[1][0]], **extra})
    assert sorted(ranked_photos(res)) == expected


@pytest.mark.parametrize("field, value", [("start", "2026-09-25"), ("end", "25/09/2026 10:00")])
def test_bad_time_is_400_naming_field(tmp_path, field, value):
    c, _, ids = filter_index(tmp_path)
    res = client(c).post("/api/search", json={"persons": [ids[1][0]], field: value})
    assert res.status_code == 400
    assert res.json()["detail"].startswith(field) and "YYYY-MM-DD HH:MM" in res.json()["detail"]


def test_validation_errors_are_one_line_400(tmp_path):
    c, _, _ = filter_index(tmp_path)
    res = client(c).post("/api/search", json={"mode": "sideways"})
    assert res.status_code == 400 and isinstance(res.json()["detail"], str) and "mode" in res.json()["detail"]


def test_partial_ocr_warning_on_bib_filter(tmp_path):
    c, conn, ids = filter_index(tmp_path)
    conn.execute("update persons set ocr_at = null where id = ?", (ids[4][0],))
    conn.commit()
    body = client(c).post("/api/search", json={"persons": [ids[1][0]], "bib": "200"}).json()
    assert body["warnings"] == ["ocr_bibs incomplete: 1 of 5 persons not read yet; bib search only covers read persons (`photofinder index --ocr` to finish)"]


def test_text_query_encodes_once_on_model_worker(tmp_path, monkeypatch):
    c, _, _ = upload_index(tmp_path)
    fakes = FakeModels(monkeypatch, texts={"red jacket": Z})
    res = client(c).post("/api/search", json={"text": " red jacket "})
    assert ranked_photos(res)[0] == "3.jpg"
    assert fakes.encoded == [["red jacket"]]


def test_scene_query_without_scene_embeddings_is_400_before_encoding(tmp_path, monkeypatch):
    c, _, _ = upload_index(tmp_path)
    fakes = FakeModels(monkeypatch, texts={"mountain": X})
    res = client(c).post("/api/search", json={"scene": "mountain"})
    assert res.status_code == 400 and "embed_scenes" in res.json()["detail"]
    assert fakes.encoded == []


def test_scene_query_with_partial_scenes_warns(tmp_path, monkeypatch):
    c, conn, _ = upload_index(tmp_path)
    add_scenes(conn, [(1, X), (2, Y)])
    FakeModels(monkeypatch, texts={"mountain": Y})
    body = client(c).post("/api/search", json={"scene": "mountain"}).json()
    assert [r["relpath"] for r in body["results"]][:2] == ["2.jpg", "1.jpg"]
    assert body["warnings"] == ["embed_scenes incomplete: 1 of 3 photos have no scene vector yet; "
                                "rerun `photofinder index`"]


def test_photo_detail_lists_all_persons_with_bibs_labels_and_size(tmp_path):
    c, conn, ids = filter_index(tmp_path)
    api = client(c)
    label(api, ids[1][1], "me")
    pid = photo_ids(conn)["1.jpg"]
    d = api.get(f"/api/photos/{pid}").json()
    assert (d["photo_id"], d["width"], d["height"], d["taken_at"], d["photographer"], d["album"]) == \
        (pid, 200, 300, "2026-09-25 08:00:00", "阿光", "9.25 赛事")
    assert d["source_photo_id"] == "1"
    assert d["persons"] == [
        {"person_id": ids[1][0], "box": [0, 0, 50, 100], "bibs": [], "label": None},
        {"person_id": ids[1][1], "box": [60, 0, 110, 100], "bibs": [{"text": "2001", "conf": 1.0}], "label": "me"}]
    assert api.get("/api/photos/999").status_code == 404


def test_images_crop_and_thumbnail(tmp_path):
    c, conn, ids = make_index(tmp_path, [(1, (60, 0, 110, 100), A, X), (2, (0, 0, 200, 300), B, Y)])
    api = client(c)
    pid = photo_ids(conn)["1.jpg"]
    full = api.get(f"/api/photos/{pid}/image")
    assert full.status_code == 200 and full.content == (c / "1.jpg").read_bytes()
    assert "max-age" in full.headers["cache-control"]
    thumb = api.get(f"/api/photos/{pid}/image", params={"max": 60})
    assert Image.open(io.BytesIO(thumb.content)).size == (40, 60)
    crop = api.get(f"/api/persons/{ids[1][0]}/crop")
    assert crop.headers["content-type"] == "image/jpeg" and "max-age" in crop.headers["cache-control"]
    img = Image.open(io.BytesIO(crop.content))
    assert img.format == "JPEG" and img.size == (60, 110)
    assert img.getpixel((30, 55)) == pytest.approx(Image.open(c / "1.jpg").getpixel((85, 45)), abs=12)
    big = Image.open(io.BytesIO(api.get(f"/api/persons/{ids[2][0]}/crop").content))
    assert big.height == 256
    assert api.get("/api/persons/999/crop").status_code == 404
    assert api.get("/api/photos/999/image").status_code == 404


def test_missing_or_unreadable_photo_file_is_404_json(tmp_path):
    c, conn, ids = make_index(tmp_path, [(1, (60, 0, 110, 100), A, X), (2, (0, 0, 50, 100), B, Y)])
    api = client(c)
    p1, p2 = photo_ids(conn)["1.jpg"], photo_ids(conn)["2.jpg"]
    (c / "1.jpg").unlink()
    (c / "2.jpg").write_bytes(b"not a jpeg")
    for url in (f"/api/photos/{p1}/image", f"/api/photos/{p1}/image?max=100", f"/api/persons/{ids[1][0]}/crop",
                f"/api/photos/{p2}/image?max=100", f"/api/persons/{ids[2][0]}/crop"):
        res = api.get(url)
        assert res.status_code == 404, url
        assert "missing or unreadable" in res.json()["detail"]


def test_my_photos_and_export(tmp_path, monkeypatch):
    c, conn, ids = filter_index(tmp_path)
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data")
    api = client(c)
    res = api.post("/api/export")
    assert res.status_code == 400 and "me" in res.json()["detail"]
    assert not (tmp_path / "data").exists()
    assert api.get("/api/me").json() == {"photos": [], "count": 0}
    label(api, ids[1][0], "me")
    label(api, ids[1][1], "me")
    label(api, ids[4][0], "me")
    label(api, ids[3][0], "not_me")
    conn.execute("update photos set source_photo_id = null where relpath = '4.jpg'")
    conn.commit()
    mine = api.get("/api/me").json()
    assert mine["count"] == 2
    assert [(p["relpath"], [q["person_id"] for q in p["persons"]]) for p in mine["photos"]] == \
        [("1.jpg", [ids[1][0], ids[1][1]]), ("4.jpg", [ids[4][0]])]
    assert mine["photos"][0]["persons"][1]["box"] == [60, 0, 110, 100]
    body = api.post("/api/export").json()
    out = tmp_path / "data" / "exports" / f"coll-{time.strftime('%Y%m%d')}.txt"
    assert body == {"path": str(out), "count": 2}
    p = photo_ids(conn)
    assert out.read_text().splitlines() == [
        "source_photo_id\tphoto_id\tpath",
        f"1\t{p['1.jpg']}\t{c.resolve() / '1.jpg'}",
        f"-\t{p['4.jpg']}\t{c.resolve() / '4.jpg'}",
    ]


def test_facets(tmp_path):
    c, conn, ids = filter_index(tmp_path)
    api = client(c)
    label(api, ids[1][0], "me")
    label(api, ids[2][0], "not_me")
    f = api.get("/api/facets").json()
    assert (f["collection"], f["photos"], f["persons"]) == ("coll", 4, 5)
    assert f["taken_at"] == {"min": "2026-09-25 08:00:00", "max": "2026-09-25 10:00:00"}
    assert f["photographers"] == [{"name": "阿光", "uid": "u1", "photos": 2}, {"name": "Lens", "uid": "u2", "photos": 1},
                                  {"name": None, "uid": "u3", "photos": 1}]
    assert f["albums"] == [{"name": "9.25 赛事", "photos": 3}, {"name": "定妆照", "photos": 1}]
    assert f["labels"] == {"me": 1, "not_me": 1}
    assert f["scenes"] is False
    assert any("embed_scenes" in w for w in f["warnings"])
    conn.execute("update persons set ocr_at = null")
    conn.commit()
    assert any("ocr_bibs" in w for w in api.get("/api/facets").json()["warnings"])
    assert api.get("/").status_code == 200


def test_serve_exits_one_line_without_index_or_embeddings(tmp_path, monkeypatch):
    import uvicorn
    runs = []
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append(kw))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", str(empty)])
    assert "no index" in str(e.value.code) and "\n" not in str(e.value.code)
    assert not (empty / db.INDEX_NAME).exists()
    c, _, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X)], embed=False)
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", str(c)])
    assert "embed_persons" in str(e.value.code) and "\n" not in str(e.value.code)
    assert runs == []


def test_serve_binds_localhost_single_worker(tmp_path, monkeypatch, capsys):
    import uvicorn
    runs = []
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append(kw))
    c, _, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X)])
    cli.main(["serve", str(c), "--port", "8765"])
    assert runs == [{"host": "127.0.0.1", "port": 8765, "workers": 1}]
    assert "http://127.0.0.1:8765/" in capsys.readouterr().out


def test_persons_are_held_as_float16(tmp_path):
    _, conn, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X), (2, (0, 0, 9, 9), B, Y)])
    add_scenes(conn, [(1, X)])
    persons = search.load_scenes(conn, search.load_persons(conn))
    assert {k: v.dtype for k, v in persons.vecs.items()} == {"osnet": np.float16, "siglip": np.float16}
    assert persons.scene[0].dtype == np.float16


def test_page_is_served_and_calls_only_real_endpoints(tmp_path):
    c, _, _ = make_index(tmp_path, [(1, (0, 0, 9, 9), A, X)])
    api = web.create_app(c)
    res = TestClient(api).get("/")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    script = re.search(r"<script>(.*)</script>", res.text, re.S).group(1)
    used = {re.sub(r"\$\{[^}]*\}", "{}", p) for p in re.findall(r"/api/[^\s'\"`?]*", script)}
    routes = {re.sub(r"\{[^}]+\}", "{}", r.path) for r in api.routes if r.path.startswith("/api/")}
    assert used == routes


def test_load_more_excludes_seen_photos_so_labelling_does_not_skip_results(tmp_path):
    c, _, ids = make_index(tmp_path, [(k, (0, 0, 50, 100), [1, 0.2 * (k - 1), 0, 0], [1, 0.2 * (k - 1), 0])
                                      for k in range(1, 7)], photos=6)
    api = client(c)
    label(api, ids[1][0], "me")
    first = api.post("/api/search", json={"mode": "more", "top": 2}).json()["results"]
    assert [r["relpath"] for r in first] == ["2.jpg", "3.jpg"]
    label(api, ids[2][0], "me")
    seen = [r["photo_id"] for r in first]
    page = api.post("/api/search", json={"mode": "more", "top": 2, "seen": seen}).json()["results"]
    assert [(r["rank"], r["relpath"]) for r in page] == [(3, "4.jpg"), (4, "5.jpg")]


def add_unembedded_person(conn, photo):
    pid = conn.execute("insert into persons(photo_id, x1, y1, x2, y2, conf) select id, 0, 0, 9, 9, 0.9 "
                       "from photos where relpath = ?", (f"{photo}.jpg",)).lastrowid
    conn.commit()
    return pid


def test_labels_on_unembedded_persons_are_skipped_with_a_warning(tmp_path):
    c, conn, ids = more_index(tmp_path)
    api = client(c)
    ghost_me, ghost_not = add_unembedded_person(conn, 3), add_unembedded_person(conn, 4)
    label(api, ghost_me, "me")
    res = api.post("/api/search", json={"mode": "more"})
    assert res.status_code == 400 and "no embeddings yet" in res.json()["detail"]
    assert api.post("/api/search", json={"persons": [ghost_me]}).status_code == 400
    label(api, ids[1][0], "me")
    label(api, ghost_not, "not_me")
    body = api.post("/api/search", json={"mode": "more"}).json()
    assert body["warnings"] == ["2 marked people have no embeddings yet; rerun `photofinder index`"]
    assert [r["relpath"] for r in body["results"]] == ["2.jpg", "4.jpg"]
    body = api.post("/api/search", json={"persons": [ids[1][0]]}).json()
    assert body["warnings"] == ["1 marked person has no embeddings yet; rerun `photofinder index`"]
    assert body["results"][0]["relpath"] == "1.jpg"


def test_label_response_carries_previous_label(tmp_path):
    c, _, ids = make_index(tmp_path, [(1, (0, 0, 50, 100), A, X)])
    api = client(c)
    pid = ids[1][0]
    seq = [label(api, pid, v).json() for v in ("me", "me", "not_me", None, None)]
    assert [(r["previous"], r["label"]) for r in seq] == \
        [(None, "me"), ("me", "me"), ("me", "not_me"), ("not_me", None), (None, None)]


def test_not_me_never_boosts_a_candidate_without_osnet(tmp_path, monkeypatch):
    nan = [float("nan")] * 4
    c, _, ids = make_index(tmp_path, [(1, (0, 0, 50, 100), nan, X), (2, (0, 0, 50, 100), A, X),
                                      (3, (0, 0, 50, 100), B, X)])
    FakeModels(monkeypatch, texts={"red jacket": X})
    api = client(c)
    label(api, ids[3][0], "not_me")
    results = api.post("/api/search", json={"text": "red jacket"}).json()["results"]
    scores = {r["relpath"]: r["score"] for r in results}
    assert results[0]["relpath"] == "2.jpg"
    assert scores["1.jpg"] == pytest.approx(scores["3.jpg"], abs=5e-3) and scores["1.jpg"] < scores["2.jpg"]


def test_ids_beyond_int64_are_400_not_500(tmp_path):
    c, _, _ = make_index(tmp_path, [(1, (0, 0, 50, 100), A, X)])
    api = client(c)
    big = 2 ** 63
    for url in (f"/api/photos/{big}", f"/api/photos/{big}/image", f"/api/persons/{big}/crop"):
        assert api.get(url).status_code == 400, url
    assert api.post("/api/labels", json={"person_id": big, "label": "me"}).status_code == 400
    assert api.post("/api/search", json={"persons": [big]}).status_code == 400
    assert api.post("/api/search", json={"persons": [1], "seen": [big]}).status_code == 400
    assert api.get(f"/api/photos/{big - 1}").status_code == 404


def test_export_quotes_fields_with_tabs_or_newlines(tmp_path, monkeypatch):
    c, conn, ids = make_index(tmp_path, [(1, (0, 0, 50, 100), A, X), (2, (0, 0, 50, 100), B, Y)])
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data")
    api = client(c)
    label(api, ids[1][0], "me")
    label(api, ids[2][0], "me")
    conn.execute("update photos set source_photo_id = 'a\tb' where relpath = '1.jpg'")
    conn.execute("update photos set relpath = 'x\ny.jpg' where relpath = '2.jpg'")
    conn.commit()
    p = photo_ids(conn)
    text = open(api.post("/api/export").json()["path"], newline="").read()
    assert list(csv.reader(io.StringIO(text), delimiter="\t")) == [
        ["source_photo_id", "photo_id", "path"],
        ["a\tb", str(p["1.jpg"]), str(c.resolve() / "1.jpg")],
        ["2", str(p["x\ny.jpg"]), str(c.resolve() / "x\ny.jpg")],
    ]


def test_upload_evicted_between_check_and_read_is_404(tmp_path, monkeypatch):
    class Evicting(web.OrderedDict):
        def __contains__(self, key):
            return True
    monkeypatch.setattr(web, "OrderedDict", Evicting)
    c, _, _ = upload_index(tmp_path)
    api = client(c)
    assert api.get("/api/uploads/gone/image").status_code == 404
    assert api.post("/api/search", json={"upload": "gone"}).status_code == 404
