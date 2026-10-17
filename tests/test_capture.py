import threading

import pytest

import heapwise

MiB = 1024 * 1024


def allocate_blocks(n, size):
    return [bytearray(size) for _ in range(n)]


def test_peak_is_attributed_to_the_allocating_function():
    with heapwise.profile() as result:
        blocks = allocate_blocks(40, 256 * 1024)  # 10 MiB
        del blocks
    cap = result[0]
    assert 10 * MiB <= cap.peak_bytes < 12 * MiB
    func, file, line, nbytes, count, _native = cap.top("peak", [], 1)[0]
    assert func == "<listcomp>" or func.endswith("allocate_blocks")
    assert file == __file__
    assert nbytes >= 10 * MiB


def test_leaks_view_shows_what_survived():
    keep = []
    with heapwise.profile() as result:
        keep.append(allocate_blocks(8, 128 * 1024))
        temp = allocate_blocks(8, 128 * 1024)
        del temp
    cap = result[0]
    assert 1 * MiB <= cap.leaked_bytes < 2 * MiB
    assert cap.top("leaks", [], 1)[0][0].endswith("allocate_blocks")


def churn(n):
    total = 0
    for i in range(n):
        total += len(str(i) * 3)
    return total


def test_temporaries_are_counted_in_exact_mode():
    with heapwise.profile() as result:
        churn(5000)
    rows = result[0].top("temporaries", [], 3)
    assert rows[0][0] == "churn"
    assert rows[0][4] >= 4000


def test_sampling_estimates_peak():
    with heapwise.profile(sample_rate=32 * 1024) as result:
        blocks = allocate_blocks(400, 64 * 1024)  # 25 MiB
        del blocks
    peak = result[0].peak_bytes
    assert 20 * MiB < peak < 30 * MiB


def test_pending_exception_is_preserved():
    with heapwise.profile():
        try:
            raise KeyError("boom")
        except KeyError as exc:
            data = [str(i) for i in range(1000)]
            assert exc.args == ("boom",)
    assert len(data) == 1000


def test_threads_are_recorded():
    def worker(out):
        out.append(allocate_blocks(4, 256 * 1024))

    out = []
    with heapwise.profile() as result:
        threads = [threading.Thread(target=worker, args=(out,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert result[0].leaked_bytes >= 4 * MiB


def test_start_twice_raises():
    heapwise.start()
    try:
        with pytest.raises(RuntimeError):
            heapwise.start()
    finally:
        heapwise.stop()


def test_summary_leads_with_the_peak():
    with heapwise.profile() as result:
        blocks = allocate_blocks(10, 512 * 1024)
        del blocks
    text = result[0].summary("test", [], None, 3)
    assert text.splitlines()[1].startswith("Peak     5.")
    assert "allocate_blocks" in text


@pytest.mark.parametrize("text,expected", [("24 MB", 24_000_000), ("512KiB", 524288), ("1.5 GiB", 1610612736), (7, 7)])
def test_parse_size(text, expected):
    assert heapwise.parse_size(text) == expected


def test_stacks_stay_distinct_when_callers_alternate(tmp_path, monkeypatch):
    # The same helper called from two places, interleaved. The per-thread
    # stack cache must not credit one caller's blocks to the other. The helper
    # lives in its own module, which we exclude so blocks go to the callers.
    (tmp_path / "hw_helper.py").write_text(
        "def alloc(n):\n    return [bytearray(4096) for _ in range(n)]\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import hw_helper

    def via_one(n):
        return hw_helper.alloc(n)

    def via_two(n):
        return hw_helper.alloc(n)

    keep = []
    with heapwise.profile() as result:
        for _ in range(50):
            keep.append(via_one(10))
            keep.append(via_two(20))
    rows = result[0].top("leaks", [str(tmp_path)], 20)
    by_func = {r[0].rsplit(".", 1)[-1]: r[3] for r in rows}
    block = 4096 + 32  # bytearray payload plus object overhead, roughly
    assert 50 * 10 * 4096 <= by_func["via_one"] < 50 * 10 * block * 1.5
    assert 50 * 20 * 4096 <= by_func["via_two"] < 50 * 20 * block * 1.5


def test_peak_time_is_within_the_run():
    import time

    with heapwise.profile() as result:
        time.sleep(0.05)
        blocks = allocate_blocks(20, 256 * 1024)
        del blocks
        time.sleep(0.05)
    cap = result[0]
    assert 0.04 < cap.peak_seconds < cap.duration_seconds
    tl = cap.timeline(10)
    assert len(tl) == 10 and max(tl) == cap.peak_bytes


def _alloc_a(n):
    return bytearray(n)


def _alloc_b(n):
    return bytearray(n)


def _alloc_c(n):
    return bytearray(n)


def _alloc_d(n):
    return bytearray(n)


def _peak_sites(cap):
    return {row[0].rsplit(".", 1)[-1]: row[3] for row in cap.top("peak", [], 20) if row[3] >= MiB}


def test_peak_view_is_what_was_live_at_the_latest_peak():
    # Peak 1: a + b. Freeing a and allocating c stays below it.
    with heapwise.profile() as result:
        a, b = _alloc_a(10 * MiB), _alloc_b(5 * MiB)
        del a
        c = _alloc_c(8 * MiB)
    assert set(_peak_sites(result[0])) == {"_alloc_a", "_alloc_b"}
    del b, c

    # Peak 2 replaces it: c + d, after a and b are gone.
    with heapwise.profile() as result:
        a, b = _alloc_a(10 * MiB), _alloc_b(5 * MiB)
        del a
        c = _alloc_c(8 * MiB)
        del b
        d = _alloc_d(20 * MiB)
    sites = _peak_sites(result[0])
    assert set(sites) == {"_alloc_c", "_alloc_d"}
    assert 28 * MiB <= result[0].peak_bytes < 29 * MiB
    del c, d


def test_long_runs_keep_a_bounded_timeline():
    with heapwise.profile() as result:
        for _ in range(200_000):
            x = bytearray(100)
            del x
    tl = result[0].timeline(1000)
    assert len(tl) == 1000
    assert max(tl) <= result[0].peak_bytes
