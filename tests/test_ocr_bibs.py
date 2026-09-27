import sqlite3
import sys
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from photofinder import db, models
from photofinder.index.stages import bib_tokens, ocr_bibs, read_bibs, scan
from photofinder.memory import AdaptiveBatcher


@pytest.mark.parametrize("text, tokens", [
    ("0887", ["0887"]),
    ("No.1685", ["1685"]),
    ("#2001", ["2001"]),
    ("15/817", ["817"]),
    ("8013 5Nl", ["8013"]),
    ("A 123 and 45678", ["123", "45678"]),
    ("12", []),
    ("157014", []),
    ("-l2100", []),
    ("157H1E7", []),
    ("i1111", []),
    ("109g", []),
    ("GLACIER EXTREME", []),
    ("１２３４", []),
])
def test_bib_tokens_keep_standalone_3_to_5_digit_runs(text, tokens):
    assert sorted(bib_tokens([(text, 0.5)])) == sorted(tokens)


def test_bib_tokens_dedupe_keeps_best_confidence():
    assert bib_tokens([("1685", 0.3), ("No.1685", 1.0), ("1685", 0.5), ("2001", 0.3)]) == {"1685": 1.0, "2001": 0.3}


class FakeReader:
    def __init__(self, texts=None, fail_on_call=None, error=KeyboardInterrupt):
        self.calls = []
        self.texts = texts or {}
        self.fail_on_call = fail_on_call
        self.error = error

    def __call__(self, img):
        self.calls.append(img.size)
        if len(self.calls) == self.fail_on_call:
            raise self.error("interrupted")
        return self.texts.get(img.size, [])


def test_read_bibs_skips_small_and_upscales_mid_crops():
    reader = FakeReader({(200, 600): [("8004", 1.0)], (100, 700): [("8043", 0.5)]})
    assert read_bibs(Image.new("RGB", (60, 199)), reader) == {}
    assert read_bibs(Image.new("RGB", (100, 300)), reader) == {"8004": 1.0}
    assert read_bibs(Image.new("RGB", (100, 700)), reader) == {"8043": 0.5}
    assert reader.calls == [(200, 600), (100, 700)]


BOXES = [(0, 0, 100, 300), (100, 0, 200, 750), (200, 0, 260, 150), (300, 0, 400, 400)]
TEXTS = {(200, 600): [("No.0887", 1.0), ("FUGA", 0.5)], (100, 750): [("1685", 0.5), ("1685", 1.0)]}


def make_collection(tmp_path):
    c = tmp_path / "coll"
    c.mkdir()
    for i in (1, 2):
        Image.new("RGB", (500, 800), "green").save(c / f"{i}.jpg", "JPEG")
    conn = db.connect(c)
    scan(conn, c)
    conn.executemany("insert into persons(photo_id, x1, y1, x2, y2, conf) values (?,?,?,?,?,0.9)",
                     [(1, *box) for box in BOXES[:3]] + [(2, *BOXES[3])])
    conn.commit()
    return c, conn


def fixed_batcher(size):
    return AdaptiveBatcher(size, reader=lambda: 1, sleep=lambda s: None)


def bibs(conn):
    return sorted(conn.execute("select person_id, text, conf from bibs"))


def not_read(conn):
    return [i for (i,) in conn.execute("select id from persons where ocr_at is null order by id")]


def test_ocr_bibs_stores_tokens_and_marks_every_person_done(tmp_path):
    c, conn = make_collection(tmp_path)
    reader = FakeReader(TEXTS)
    counts = ocr_bibs(conn, c, reader=reader, batcher=fixed_batcher(8))
    assert counts == {"pending": 4, "persons": 4, "bibs": 2, "errors": 0, "ocr_errors": 0}
    assert bibs(conn) == [(1, "0887", 1.0), (2, "1685", 1.0)]
    assert reader.calls == [(200, 600), (100, 750), (200, 800)]
    assert not_read(conn) == []

    idle = FakeReader(TEXTS)
    assert ocr_bibs(conn, c, reader=idle)["pending"] == 0
    assert idle.calls == []
    assert len(bibs(conn)) == 2


def test_ocr_bibs_resumes_after_crash_without_duplicates(tmp_path):
    c, conn = make_collection(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        ocr_bibs(conn, c, reader=FakeReader(TEXTS, fail_on_call=2), batcher=fixed_batcher(1))
    assert bibs(conn) == [(1, "0887", 1.0)]
    assert not_read(conn) == [2, 3, 4]
    again = FakeReader(TEXTS)
    ocr_bibs(conn, c, reader=again, batcher=fixed_batcher(8))
    assert again.calls == [(100, 750), (200, 800)]
    assert bibs(conn) == [(1, "0887", 1.0), (2, "1685", 1.0)]


def test_ocr_bibs_failure_mid_transaction_rolls_back_batch(tmp_path):
    c, conn = make_collection(tmp_path)
    conn.execute("""create trigger boom before update of ocr_at on persons when new.id = 3
                    begin select raise(abort, 'boom'); end""")
    with pytest.raises(sqlite3.IntegrityError):
        ocr_bibs(conn, c, reader=FakeReader(TEXTS), batcher=fixed_batcher(8))
    assert bibs(conn) == []
    assert not_read(conn) == [1, 2, 3, 4]
    conn.execute("drop trigger boom")
    assert ocr_bibs(conn, c, reader=FakeReader(TEXTS))["bibs"] == 2
    assert len(bibs(conn)) == 2


def test_ocr_bibs_unreadable_photo_marks_error_and_skips_its_persons(tmp_path):
    c, conn = make_collection(tmp_path)
    (c / "1.jpg").write_bytes(b"gone bad")
    reader = FakeReader(TEXTS)
    counts = ocr_bibs(conn, c, reader=reader, batcher=fixed_batcher(8))
    assert counts == {"pending": 4, "persons": 1, "bibs": 0, "errors": 1, "ocr_errors": 0}
    assert reader.calls == [(200, 800)]
    status, error = conn.execute("select status, error from photos where relpath = '1.jpg'").fetchone()
    assert status == "error" and error
    assert not_read(conn) == [1, 2, 3]
    assert ocr_bibs(conn, c, reader=FakeReader(TEXTS))["pending"] == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision")
def test_read_text_reads_digits_with_apple_vision():
    img = Image.new("RGB", (600, 260), "white")
    ImageDraw.Draw(img).text((40, 60), "0887", fill="black", font=ImageFont.load_default(size=120))
    found = models.read_text(img)
    assert "0887" in bib_tokens(found)
    assert all(isinstance(t, str) and 0 <= c <= 1 for t, c in found)


def test_ocr_failure_leaves_person_pending_and_photo_ok(tmp_path, caplog):
    c, conn = make_collection(tmp_path)
    reader = FakeReader(TEXTS, fail_on_call=2, error=RuntimeError)
    counts = ocr_bibs(conn, c, reader=reader, batcher=fixed_batcher(8))
    assert counts == {"pending": 4, "persons": 3, "bibs": 1, "errors": 0, "ocr_errors": 1}
    assert not_read(conn) == [2]
    assert bibs(conn) == [(1, "0887", 1.0)]
    assert conn.execute("select distinct status from photos").fetchall() == [("ok",)]
    assert any("person 2" in r.getMessage() and "pending" in r.getMessage() for r in caplog.records)

    again = FakeReader(TEXTS)
    assert ocr_bibs(conn, c, reader=again)["persons"] == 1
    assert again.calls == [(100, 750)]
    assert not_read(conn) == []
    assert bibs(conn) == [(1, "0887", 1.0), (2, "1685", 1.0)]


class FakeVision:
    VNRequestTextRecognitionLevelAccurate = 0

    def __init__(self, ret, texts=()):
        self.ret, self.texts, self.req = ret, texts, None
        vision = self

        class Request:
            @staticmethod
            def alloc():
                return Request()

            def init(self):
                vision.req = self
                return self

            def setRecognitionLevel_(self, level):
                self.level = level

            def setRecognitionLanguages_(self, langs):
                self.langs = langs

            def results(self):
                return [SimpleNamespace(text=lambda t=t: t, confidence=lambda c=c: c) for t, c in vision.texts]

        class Handler:
            @staticmethod
            def alloc():
                return Handler()

            def initWithData_options_(self, data, options):
                assert data.startswith(b"\x89PNG")
                return self

            def performRequests_error_(self, reqs, err):
                return vision.ret

        self.VNRecognizeTextRequest, self.VNImageRequestHandler = Request, Handler


@pytest.mark.parametrize("ret", [(False, "NSError boom"), (True, "NSError boom"), False])
def test_read_text_raises_when_vision_fails(monkeypatch, ret):
    monkeypatch.setitem(sys.modules, "Vision", FakeVision(ret, [("1685", 1.0)]))
    with pytest.raises(RuntimeError, match="Vision"):
        models.read_text(Image.new("RGB", (40, 30)))


@pytest.mark.parametrize("ret", [(True, None), True])
def test_read_text_returns_text_and_conf_on_success(monkeypatch, ret):
    vision = FakeVision(ret, [("No.1685", 0.5), ("FUGA", 1.0)])
    monkeypatch.setitem(sys.modules, "Vision", vision)
    assert models.read_text(Image.new("RGB", (40, 30))) == [("No.1685", 0.5), ("FUGA", 1.0)]
    assert vision.req.level == 0 and vision.req.langs == ["en-US"]
