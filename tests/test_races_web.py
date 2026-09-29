import shutil
import sqlite3
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from photofinder import cli, config, db, originals, races, search
from photofinder.sources import yipai
from photofinder.web import app as web
from test_originals import ORDER, Gallery
from test_search import A, B, C, X, Y, Z, make_index
from test_web import ME, FakeModels, jpeg_bytes, no_real_models  # noqa: F401
from test_yipai import FakeTime

RA, RB, RC, RD = "2026-a", "2026-b", "2026-c", "2026-d"


def indexed_race(tmp_path, slug, name, persons, photos, embed=True):
    work = tmp_path / f"build-{slug}"
    work.mkdir()
    c, conn, ids = make_index(work, persons, photos=photos, embed=embed)
    conn.close()
    races.add_race(slug, name)
    d = races.race_dir(slug)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(c, d)
    return d, ids


def manifest(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def yipai_manifest(path, statuses, view=True):
    m = manifest(path)
    m.executescript(yipai.SCHEMA)
    if not view:
        m.execute("drop view catalog")
    m.executemany("insert into photos(photo_id, order_id, fname, status) values (?,?,?,?)",
                  [(i, ORDER, f"{i}.JPG", s) for i, s in enumerate(statuses, 1)])
    m.commit()
    m.close()


def catalog_manifest(path, statuses):
    m = manifest(path)
    m.execute("create table catalog(source_id text primary key, status text)")
    m.executemany("insert into catalog values (?,?)", [(str(i), s) for i, s in enumerate(statuses)])
    m.commit()
    m.close()


def album(slug, key):
    return races.race_dir(slug) / "albums" / key / "manifest.sqlite"


@pytest.fixture
def world(tmp_path):
    a, ids_a = indexed_race(tmp_path, RA, "Race A", [(1, (0, 0, 50, 100), A, X), (2, (0, 0, 50, 100), B, Y),
                                                     (3, (0, 0, 50, 100), C, Z)], photos=3)
    b, ids_b = indexed_race(tmp_path, RB, "Race B", [(1, (0, 0, 50, 100), A, X)], photos=1)
    races.add_race(RC, "Race C")
    races.race_dir(RC).mkdir(parents=True)
    indexed_race(tmp_path, RD, "Race D", [(1, (0, 0, 50, 100), A, X)], photos=1, embed=False)
    races.add_album(RA, "https://www.yipai360.com/photolivepc/?orderId=1001", "Yipai One")
    races.add_album(RA, "https://www.yipai360.com/photolivepc/?orderId=1002")
    races.add_album(RB, "https://www.xxpie.com/m/album?album_id=abc", "Xx")
    races.add_album(RB, "https://live.photoplus.cn/live/39352660", "Pp")
    races.add_album(RB, "https://www.yipai360.com/photolivepc/?orderId=1003", "Legacy")
    yipai_manifest(album(RA, "yipai-1001"), ["done", "done", "missing", "pending"])
    catalog_manifest(album(RB, "xxpie-abc"), ["done", "done", "done", "failed"])
    album(RB, "photoplus-39352660").parent.mkdir(parents=True)
    album(RB, "photoplus-39352660").write_bytes(b"not a database at all" * 100)
    yipai_manifest(album(RB, "yipai-1003"), ["done", "pending"], view=False)
    return {"a": a, "b": b, "ids_a": ids_a, "ids_b": ids_b}


def api_of(**kw):
    return TestClient(web.create_app(**kw))


def load(api, slug):
    return api.post(f"/api/races/{slug}/load")


def by_slug(api):
    return {r["slug"]: r for r in api.get("/api/races").json()}


def labels_in(d):
    with sqlite3.connect(d / db.INDEX_NAME) as conn:
        return conn.execute("select person_id, label from labels").fetchall()


def test_races_lists_registry_with_indexed_loaded_and_downloaded_counts(world):
    api = api_of()
    got = by_slug(api)
    assert list(got) == [RA, RB, RC, RD]
    assert {s: (r["name"], r["indexed"], r["loaded"]) for s, r in got.items()} == {
        RA: ("Race A", True, False), RB: ("Race B", True, False), RC: ("Race C", False, False),
        RD: ("Race D", False, False)}
    assert got[RA]["albums"] == [
        {"key": "yipai-1001", "platform": "yipai", "title": "Yipai One",
         "url": "https://www.yipai360.com/photolivepc/?orderId=1001", "downloaded": 2},
        {"key": "yipai-1002", "platform": "yipai", "title": None,
         "url": "https://www.yipai360.com/photolivepc/?orderId=1002", "downloaded": 0}]
    assert [(a["key"], a["downloaded"]) for a in got[RB]["albums"]] == [
        ("xxpie-abc", 3), ("photoplus-39352660", 0), ("yipai-1003", 1)]
    assert got[RC]["albums"] == []
    assert load(api, RA).status_code == 200
    assert [s for s, r in by_slug(api).items() if r["loaded"]] == [RA]


def test_load_returns_facets_and_race_routes_answer_only_for_the_loaded_race(world):
    api = api_of()
    res = api.get(f"/api/r/{RA}/facets")
    assert res.status_code == 409 and res.json()["loaded"] is None and "reload" in res.json()["detail"]
    body = load(api, RA).json()
    assert body == api.get(f"/api/r/{RA}/facets").json()
    assert body["race"] == {"slug": RA, "name": "Race A"} and body["photos"] == 3 and body["persons"] == 3
    stale = api.get(f"/api/r/{RB}/facets")
    assert stale.status_code == 409
    assert stale.json() == {"detail": "The server switched to Race A — reload", "loaded": RA}
    assert api.get("/api/facets").status_code == 404


def snapshot(*dirs):
    out = []
    for d in dirs:
        with closing(sqlite3.connect(d / db.INDEX_NAME)) as conn:
            out.append(list(conn.iterdump()))
    exports = config.DATA_ROOT / "exports"
    out.append(sorted((str(p), p.stat().st_size) for p in exports.rglob("*")) if exports.exists() else None)
    return out


def test_stale_slug_is_refused_before_any_write(world):
    api = api_of()
    load(api, RA)
    before = snapshot(world["a"], world["b"])
    pid = world["ids_b"][1][0]
    for method, path, body in [("post", "labels", {"profile_id": ME, "person_id": pid, "label": "me"}),
                               ("post", "profiles", {"name": "Zed"}),
                               ("patch", f"profiles/{ME}", {"name": "Zed"}),
                               ("delete", f"profiles/{ME}", None),
                               ("post", "export", {"profile_id": ME}),
                               ("post", "originals", {"profile_id": ME})]:
        res = api.request(method.upper(), f"/api/r/{RB}/{path}", json=body)
        assert res.status_code == 409 and res.json()["loaded"] == RA, (path, res.text)
    assert snapshot(world["a"], world["b"]) == before
    assert labels_in(world["b"]) == []
    with sqlite3.connect(world["b"] / db.INDEX_NAME) as conn:
        assert conn.execute("select name from profiles").fetchall() == [("Me",)]
    assert not (config.DATA_ROOT / "exports").exists()


def test_switching_a_b_a_shows_each_races_own_data(world):
    api = api_of()
    load(api, RA)
    pa = world["ids_a"][2][0]
    assert api.post(f"/api/r/{RA}/labels", json={"profile_id": ME, "person_id": pa, "label": "me"}).status_code == 200
    b = load(api, RB).json()
    assert (b["race"]["slug"], b["photos"]) == (RB, 1)
    assert api.get(f"/api/r/{RB}/me", params={"profile_id": ME}).json()["count"] == 0
    assert load(api, RA).json()["photos"] == 3
    me = api.get(f"/api/r/{RA}/me", params={"profile_id": ME}).json()
    assert [p["relpath"] for p in me["photos"]] == ["2.jpg"]
    assert labels_in(world["b"]) == []


def test_loading_the_loaded_race_is_a_no_op(world, monkeypatch):
    api = api_of()
    load(api, RA)
    calls = []
    monkeypatch.setattr(search, "load_persons", lambda conn, _real=search.load_persons: calls.append(1) or _real(conn))
    state = api.app.state.current
    res = load(api, RA)
    assert res.status_code == 200 and res.json()["race"]["slug"] == RA
    assert calls == [] and api.app.state.current is state


def test_unknown_and_unindexed_races_are_refused_and_keep_the_loaded_race(world):
    api = api_of()
    load(api, RA)
    res = load(api, "2026-nope")
    assert res.status_code == 404
    res = load(api, RC)
    assert res.status_code == 400 and "photofinder index 2026-c" in res.json()["detail"]
    assert not (races.race_dir(RC) / db.INDEX_NAME).exists()
    assert api.get(f"/api/r/{RA}/facets").status_code == 200


def test_race_without_embeddings_is_refused_before_the_loaded_race_is_dropped(world, monkeypatch):
    api = api_of()
    load(api, RA)
    state = api.app.state.current
    calls = []
    monkeypatch.setattr(search, "load_persons", lambda conn, _real=search.load_persons: calls.append(1) or _real(conn))
    res = load(api, RD)
    assert res.status_code == 400 and "run `photofinder index 2026-d` first" in res.json()["detail"]
    assert calls == [] and api.app.state.current is state
    assert [s for s, r in by_slug(api).items() if r["loaded"]] == [RA]
    assert api.get(f"/api/r/{RA}/facets").status_code == 200


def test_embedded_check_tolerates_missing_and_broken_indexes(world, tmp_path):
    assert web.embedded(world["a"]) and not web.embedded(races.race_dir(RD))
    assert not web.embedded(races.race_dir(RC)) and not web.embedded(tmp_path / "nowhere")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / db.INDEX_NAME).write_bytes(b"not a database at all" * 100)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    with closing(sqlite3.connect(legacy / db.INDEX_NAME)) as conn:
        conn.execute("create table photos(id integer primary key)")
    assert not web.embedded(broken) and not web.embedded(legacy)


def test_embeddings_missing_after_the_drop_names_the_slug_and_any_indexed_race_loads_afterwards(world, monkeypatch):
    api = api_of()
    load(api, RA)
    monkeypatch.setattr(web, "embedded", lambda d: True)
    res = load(api, RD)
    detail = res.json()["detail"]
    assert res.status_code == 400 and "embed_persons" in detail
    assert "photofinder index 2026-d" in detail and "<collection>" not in detail
    assert not any(r["loaded"] for r in api.get("/api/races").json())
    assert api.get(f"/api/r/{RA}/facets").json() == {"detail": "No race is loaded on the server — reload",
                                                     "loaded": None}
    assert load(api, RB).status_code == 200
    assert api.get(f"/api/r/{RB}/facets").status_code == 200


def test_old_race_state_is_dropped_before_the_next_loads_and_concurrent_load_is_409(world, monkeypatch):
    api = api_of()
    load(api, RA)
    old = weakref.ref(api.app.state.current)
    entered, release = threading.Event(), threading.Event()
    real = search.load_persons

    def held(conn):
        entered.set()
        assert release.wait(10)
        return real(conn)
    monkeypatch.setattr(search, "load_persons", held)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(load, api, RB)
        assert entered.wait(10)
        assert old() is None
        assert api.get(f"/api/r/{RA}/facets").json()["loaded"] is None
        second = load(api, RA)
        assert second.status_code == 409 and "loading" in second.json()["detail"]
        release.set()
        assert first.result(10).json()["race"]["slug"] == RB
    assert api.app.state.current.slug == RB


def test_upload_cache_is_cleared_on_switch(world, monkeypatch):
    FakeModels(monkeypatch, boxes=[(10.0, 10.0, 40.0, 60.0, 0.9)], osnet=[A, A], siglip=[X, X])
    api = api_of()
    load(api, RA)
    token = api.post(f"/api/r/{RA}/upload", files={"file": ("q.jpg", jpeg_bytes(), "image/jpeg")}).json()["token"]
    query = {"profile_id": ME, "upload": token, "box": 0}
    assert api.get(f"/api/r/{RA}/uploads/{token}/image").status_code == 200
    assert api.post(f"/api/r/{RA}/search", json=query).status_code == 200
    load(api, RB)
    assert api.post(f"/api/r/{RA}/search", json=query).status_code == 409
    res = api.post(f"/api/r/{RB}/search", json=query)
    assert res.status_code == 404 and "upload" in res.json()["detail"]
    load(api, RA)
    assert api.post(f"/api/r/{RA}/search", json=query).status_code == 404
    assert api.get(f"/api/r/{RA}/uploads/{token}/image").status_code == 404


def test_label_that_straddles_a_switch_writes_the_race_it_named(world, monkeypatch):
    api = api_of()
    load(api, RA)
    entered, release = threading.Event(), threading.Event()
    real = web.now

    def held():
        entered.set()
        assert release.wait(10)
        return real()
    monkeypatch.setattr(web, "now", held)
    pa = world["ids_a"][3][0]
    with ThreadPoolExecutor(1) as pool:
        res = pool.submit(api.post, f"/api/r/{RA}/labels", json={"profile_id": ME, "person_id": pa, "label": "me"})
        assert entered.wait(10)
        assert load(api, RB).status_code == 200
        release.set()
        assert res.result(10).status_code == 200
    assert labels_in(world["a"]) == [(pa, "me")] and labels_in(world["b"]) == []


def test_search_that_straddles_a_switch_ranks_the_race_it_named(world, monkeypatch):
    fakes = FakeModels(monkeypatch, texts={"red": X})
    entered, release = threading.Event(), threading.Event()
    real = fakes.encode

    def held(texts):
        entered.set()
        assert release.wait(10)
        return real(texts)
    monkeypatch.setattr(web.models, "encode_text", held)
    api = api_of()
    load(api, RA)
    with ThreadPoolExecutor(1) as pool:
        res = pool.submit(api.post, f"/api/r/{RA}/search", json={"profile_id": ME, "text": "red"})
        assert entered.wait(10)
        assert load(api, RB).status_code == 200
        release.set()
        body = res.result(10)
    assert body.status_code == 200, body.text
    assert sorted(r["relpath"] for r in body.json()["results"]) == ["1.jpg", "2.jpg", "3.jpg"]


def originals_race(tmp_path):
    d, ids = indexed_race(tmp_path, "2026-o", "Race O", [(1, (0, 0, 50, 100), A, X)], photos=1)
    m = sqlite3.connect(d / "manifest.sqlite")
    m.executescript(yipai.SCHEMA)
    m.execute("insert into photos(photo_id, order_id, fname) values (1, ?, 'A1.JPG')", (ORDER,))
    m.commit()
    m.close()
    t = FakeTime()
    g = Gallery(t)
    g.add(1, "A1.JPG")
    fetcher = originals.Fetcher(httpx.Client(transport=httpx.MockTransport(g)), sleep=t.sleep, clock=t.clock)
    api = api_of(fetcher=fetcher)
    assert load(api, "2026-o").status_code == 200
    person = ids[1][0]
    assert api.post("/api/r/2026-o/labels", json={"profile_id": ME, "person_id": person, "label": "me"}).status_code == 200
    photo = sqlite3.connect(d / db.INDEX_NAME).execute("select id from photos").fetchone()[0]
    return api, g, photo


def test_load_is_refused_while_the_current_races_originals_job_runs(world, tmp_path):
    api, g, _ = originals_race(tmp_path)
    entered, release = threading.Event(), threading.Event()
    g.on_image = lambda pid: entered.set() or release.wait(10)
    res = api.post("/api/r/2026-o/originals", json={"profile_id": ME})
    assert res.status_code == 200 and entered.wait(10)
    refused = load(api, RA)
    assert refused.status_code == 409 and "Race O" in refused.json()["detail"]
    assert api.app.state.current.slug == "2026-o"
    release.set()
    api.app.state.originals.thread.join(10)
    assert api.get("/api/r/2026-o/originals").json()["state"] == "done"
    old = api.app.state.current
    assert load(api, RA).status_code == 200
    assert api.post("/api/r/2026-o/originals", json={"profile_id": ME}).status_code == 409
    with pytest.raises(originals.Busy):
        old.job.claim()


def test_load_is_refused_while_a_single_original_downloads(world, tmp_path):
    api, g, photo = originals_race(tmp_path)
    entered, release = threading.Event(), threading.Event()
    g.on_image = lambda pid: entered.set() or release.wait(10)
    with ThreadPoolExecutor(1) as pool:
        single = pool.submit(api.post, f"/api/r/2026-o/photos/{photo}/original", json={"profile_id": ME})
        assert entered.wait(10)
        assert api.get("/api/r/2026-o/originals").json()["state"] == "idle"
        refused = load(api, RA)
        release.set()
        assert single.result(10).status_code == 200
    assert refused.status_code == 409 and "in progress" in refused.json()["detail"]
    assert load(api, RA).status_code == 200


def test_path_mode_serves_the_one_directory_as_the_only_race(tmp_path, monkeypatch):
    c, _, _ = make_index(tmp_path, [(1, (0, 0, 50, 100), A, X)], photos=1)
    calls = []
    monkeypatch.setattr(search, "load_persons", lambda conn, _real=search.load_persons: calls.append(1) or _real(conn))
    api = TestClient(web.create_app(c))
    assert api.get("/api/races").json() == [{"slug": "coll", "name": "coll", "indexed": True, "loaded": True,
                                             "albums": []}]
    assert load(api, "coll").json()["race"] == {"slug": "coll", "name": "coll"}
    assert load(api, RA).status_code == 404
    assert calls == [1]


def test_path_mode_without_embeddings_names_the_directory(tmp_path):
    c, conn, _ = make_index(tmp_path, [(1, (0, 0, 50, 100), A, X)], photos=1, embed=False)
    conn.close()
    with pytest.raises(search.MissingEmbeddings) as e:
        web.create_app(c)
    assert f"photofinder index {c.resolve()}`" in str(e.value)


def serve_runs(monkeypatch):
    import uvicorn
    runs = []
    monkeypatch.setattr(cli.config, "setup_model_env", lambda: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append((app, kw)))
    return runs


def test_serve_without_argument_starts_the_race_picker(world, monkeypatch, capsys):
    runs = serve_runs(monkeypatch)
    lock = races.registry_path().parent / cli.SERVE_LOCK_NAME
    seen = []
    monkeypatch.setattr(cli, "lock_serve", lambda where, port, _real=cli.lock_serve: seen.append(where) or _real(where, port))
    cli.main(["serve", "--port", "8767"])
    [(app, kw)] = runs
    assert kw == {"host": "127.0.0.1", "port": 8767, "workers": 1}
    assert app.state.current is None and seen == ["race picker"]
    assert "race picker" in lock.read_text()
    assert "the race picker at http://127.0.0.1:8767/" in capsys.readouterr().out
    api = TestClient(app)
    assert [r["slug"] for r in api.get("/api/races").json()] == [RA, RB, RC, RD]
    assert load(api, RB).status_code == 200


def test_serve_with_a_slug_opens_the_picker_with_that_race_loaded(world, monkeypatch):
    runs = serve_runs(monkeypatch)
    cli.main(["serve", RB])
    [(app, _)] = runs
    api = TestClient(app)
    got = api.get("/api/races").json()
    assert [(r["slug"], r["loaded"]) for r in got] == [(RA, False), (RB, True), (RC, False), (RD, False)]
    assert "race picker (2026-b)" in (races.registry_path().parent / cli.SERVE_LOCK_NAME).read_text()
    assert load(api, RA).status_code == 200


def test_serve_with_an_unready_slug_exits_one_line_before_serving(world, monkeypatch):
    runs = serve_runs(monkeypatch)
    for slug, text in ((RC, "no index"), (RD, "embed_persons")):
        with pytest.raises(SystemExit) as e:
            cli.main(["serve", slug])
        assert text in str(e.value.code) and "\n" not in str(e.value.code)
        assert f"run `photofinder index {slug}`" in e.value.code
    assert runs == []


def test_serve_with_a_directory_serves_it_alone(world, monkeypatch):
    runs = serve_runs(monkeypatch)
    cli.main(["serve", str(world["a"])])
    [(app, _)] = runs
    got = TestClient(app).get("/api/races").json()
    assert [(r["slug"], r["name"], r["loaded"]) for r in got] == [(RA, "Race A", True)]
    assert [a["key"] for a in got[0]["albums"]] == ["yipai-1001", "yipai-1002"]
    assert str(world["a"]) in (races.registry_path().parent / cli.SERVE_LOCK_NAME).read_text()
