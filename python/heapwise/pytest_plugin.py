"""pytest integration: ``@pytest.mark.limit_memory("24 MB")`` fails a test whose
Python heap peak goes over budget."""

from __future__ import annotations

import os

import pytest

from . import default_exclude, format_bytes, is_recording, parse_size, start, stop


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "limit_memory(size): fail if the test's peak Python heap use exceeds size, e.g. '24 MB'",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    marker = item.get_closest_marker("limit_memory")
    if marker is None or is_recording():
        yield
        return
    limit = parse_size(marker.args[0])
    start(0)
    try:
        outcome = yield
    finally:
        capture = stop()
    if outcome.excinfo is not None:
        return
    if capture.peak_bytes > limit:
        rows = capture.top("peak", default_exclude(), 5)
        cwd = os.getcwd()
        lines = [
            f"  {format_bytes(b):>10}  {func}  {os.path.relpath(file, cwd) if file else ''}:{line}"
            for func, file, line, b, _, _ in rows
        ]
        pytest.fail(
            f"Test used {format_bytes(capture.peak_bytes)} at peak, over its "
            f"{format_bytes(limit)} limit. Largest allocations at the peak:\n" + "\n".join(lines),
            pytrace=False,
        )
