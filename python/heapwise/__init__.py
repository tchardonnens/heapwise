"""heapwise: find what is using your Python program's memory."""

from __future__ import annotations

import contextlib
import os
import sysconfig
from collections.abc import Iterator

from ._native import Capture, format_bytes, is_recording, start, stop

__all__ = [
    "Capture",
    "DEFAULT_SAMPLE_RATE",
    "default_exclude",
    "format_bytes",
    "is_recording",
    "parse_size",
    "profile",
    "start",
    "stop",
    "write_html",
]

#: Mean bytes between samples in the default (sampling) mode.
DEFAULT_SAMPLE_RATE = 32 * 1024

_UNITS = {
    "b": 1,
    "kb": 1000, "mb": 1000**2, "gb": 1000**3,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3,
    "k": 1024, "m": 1024**2, "g": 1024**3,
}


def parse_size(text: str | int) -> int:
    """Parse "24 MB", "512KiB" or a plain integer into bytes."""
    if isinstance(text, int):
        return text
    s = text.strip().lower().replace(" ", "")
    num = s.rstrip("abcdefghijklmnopqrstuvwxyz")
    unit = s[len(num):] or "b"
    if unit not in _UNITS or not num:
        raise ValueError(f"cannot parse size {text!r}")
    return int(float(num) * _UNITS[unit])


def default_exclude() -> list[str]:
    """Path prefixes treated as "not your code" when attributing allocations."""
    paths = sysconfig.get_paths()
    prefixes = {paths.get(k, "") for k in ("stdlib", "platstdlib", "purelib", "platlib")}
    prefixes.add(os.path.dirname(os.path.abspath(__file__)))
    return sorted(p for p in prefixes if p)


def write_html(capture: Capture, path, title: str = "heapwise", exclude=None, cwd=None, notes=None, rss=None) -> str:
    """Write a self-contained HTML report (flame graph, call tree, timeline)."""
    # Imported here so that ``import heapwise`` doesn't pull json etc. into
    # the program being measured.
    from .html import write_html as _write

    return _write(capture, path, title, exclude, cwd, notes, rss)


@contextlib.contextmanager
def profile(sample_rate: int = 0) -> Iterator[list[Capture]]:
    """Record allocations inside a ``with`` block.

    The yielded list holds the ``Capture`` once the block exits::

        with heapwise.profile() as result:
            work()
        print(result[0].peak_bytes)
    """
    holder: list[Capture] = []
    start(sample_rate)
    try:
        yield holder
    finally:
        holder.append(stop())
