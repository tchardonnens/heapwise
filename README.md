# heapwise

A Python memory profiler that tells you what is using your memory. Rust core,
`pip install`, no code changes.

```
$ heapwise run arrays.py

heapwise · arrays.py · 0.48 s · sampled, one per 32.0 KiB on average · Python + native

Peak 104.2 MiB (95.2 MiB native, 9.1 MiB Python) at 0.33 s. 59% of it was allocated by normalise() at arrays.py:13.
Still allocated at exit: 14.2 MiB, most of it from <module>() at arrays.py:3.
Memory over time  ▁▁▁▁▂▂▂▂▄▄▄▄▄▄▄▄▄▄▄▄▆▆▆▆▆▆▆▆▆▆▆▆█████████████▆█▆  peak 104.2 MiB

Peak: what was live at the high-water mark
   58.6%    61.0 MiB    61.0 MiB native  normalise            arrays.py:13
   30.8%    32.1 MiB    31.2 MiB native  load_matrix          arrays.py:9
...
```

It sees Python objects (including pymalloc's small objects) on every OS, and
on Linux also `malloc`/`mmap` from C extensions and the libraries they load.

The same run also writes `heapwise-<script>.html`, one self-contained file
that works offline. Here it is for [`examples/pipeline.py`](https://github.com/tchardonnens/heapwise/blob/main/examples/pipeline.py)
(`heapwise run --exact examples/pipeline.py`):

![heapwise report: key numbers, memory over time, and a cascade of what was using memory at the peak](https://raw.githubusercontent.com/tchardonnens/heapwise/main/docs/images/report-overview.png)

The tiles at the top give the key numbers: the peak and who held it, the
biggest single site, what was left at exit, temporaries and total churn.
Click a tile to jump to its cascade, and hover the memory chart for the value
at any moment.

Read the cascade top to bottom. Each box is a function, as wide as the memory
it accounts for, and it splits exactly into the functions it called plus what
it allocated itself (the lighter boxes). Color says who allocated the memory:
blue for your code, orange for third-party libraries, gray for the standard
library and green for C code such as numpy's buffers. The bar above it gives
the same split in numbers; click an owner to highlight its boxes. Peak, Leaks
and Temporaries each get their own cascade, and library internals are grouped
behind the call you made into them unless you untick the box.

<p align="center">
  <img src="https://raw.githubusercontent.com/tchardonnens/heapwise/main/docs/images/report-cascade-dark.png" alt="The cascade in dark mode, with a tooltip on main" width="49%">
  <img src="https://raw.githubusercontent.com/tchardonnens/heapwise/main/docs/images/report-call-tree.png" alt="The call tree: every row's total is the sum of the rows under it" width="49%">
</p>

Hover a box for its file and lines and its share of the total and of its
caller, or click it to zoom in. The call tree below shows the same numbers as
rows.

## Install

Install it into the environment your program runs in, so it can import your
dependencies. Wheels cover Linux, macOS and Windows on Python 3.12+, no Rust
needed.

```bash
pip install heapwise
uv add --dev heapwise                          # or, in a uv project
uv run --with heapwise heapwise run script.py  # or, try it without installing
```

## Use

```bash
heapwise run script.py [args...]      # sampled, cheap
heapwise run --exact script.py        # every allocation, counts temporaries
heapwise run -m package.module
heapwise run --html out.html script.py   # choose where the report goes
heapwise run --no-html script.py         # terminal summary only
```

```python
import heapwise

with heapwise.profile() as result:
    work()
print(heapwise.format_bytes(result[0].peak_bytes))
print(result[0].top("peak", heapwise.default_exclude(), 5))
heapwise.write_html(result[0], "report.html")
```

```python
import pytest

@pytest.mark.limit_memory("24 MB")
def test_import_is_light():
    ...
```

## Develop

```bash
python -m venv .venv && . .venv/bin/activate
pip install maturin pytest
maturin develop --release
pytest
```

See [docs/ARCHITECTURE.md](https://github.com/tchardonnens/heapwise/blob/main/docs/ARCHITECTURE.md) for the design and roadmap.

## License

Apache License 2.0. See [LICENSE](LICENSE).
