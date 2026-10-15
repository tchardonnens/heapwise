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
    assert text.splitlines()[2].startswith("Peak 5.")
    assert "allocate_blocks" in text


@pytest.mark.parametrize("text,expected", [("24 MB", 24_000_000), ("512KiB", 524288), ("1.5 GiB", 1610612736), (7, 7)])
def test_parse_size(text, expected):
    assert heapwise.parse_size(text) == expected
