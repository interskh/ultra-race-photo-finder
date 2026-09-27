import logging

from photofinder.memory import AdaptiveBatcher


def batcher(levels, max_size=8, **kw):
    it = iter(levels)
    slept = []
    return AdaptiveBatcher(max_size, reader=lambda: next(it), sleep=slept.append, **kw), slept


def test_warn_halves_and_floors_at_one():
    b, _ = batcher([2] * 6)
    assert [b.next_size() for _ in range(6)] == [4, 2, 1, 1, 1, 1]


def test_critical_pauses_until_pressure_drops(caplog):
    b, slept = batcher([4, 4, 4, 1], poll=5.0)
    with caplog.at_level(logging.INFO, logger="memory"):
        assert b.next_size() == 8
    assert slept == [5.0, 5.0, 5.0] and b.batches == 1
    assert sum("paused: level=critical" in r.getMessage() for r in caplog.records) == 3


def test_grows_back_after_20_normal_batches_capped_at_max():
    b, _ = batcher([2, 2] + [1] * 60)
    assert b.next_size() == 4 and b.next_size() == 2
    sizes = [b.next_size() for _ in range(60)]
    assert sizes[:19] == [2] * 19
    assert sizes[19] == 4
    assert sizes[38] == 4 and sizes[39] == 8
    assert sizes[40:] == [8] * 20


def test_warn_resets_normal_streak():
    b, _ = batcher([2] + [1] * 19 + [2] + [1] * 19)
    sizes = [b.next_size() for _ in range(40)]
    assert sizes[19] == 4 and sizes[20] == 2 and sizes[-1] == 2


def test_logs_level_batch_and_rss(caplog):
    b, _ = batcher([1, 1, 2], log_every=2)
    with caplog.at_level(logging.INFO, logger="memory"):
        b.next_size()
        assert not caplog.records
        b.next_size()
        b.next_size()
    messages = [r.getMessage() for r in caplog.records]
    assert "level=normal batch=8 max_rss=" in messages[0]
    assert "level=warn batch=4 max_rss=" in messages[1]
    assert messages[1].endswith("MB")


def test_chunks_cover_all_items_with_adaptive_sizes():
    b, _ = batcher([1, 2, 2, 1, 1, 1], max_size=4)
    assert list(b.chunks(range(10))) == [[0, 1, 2, 3], [4, 5], [6], [7], [8], [9]]
