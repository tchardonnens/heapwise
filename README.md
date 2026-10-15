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

## Use

```bash
heapwise run script.py [args...]      # sampled, cheap
heapwise run --exact script.py        # every allocation, counts temporaries
heapwise run -m package.module
```

```python
import heapwise

with heapwise.profile() as result:
    work()
print(heapwise.format_bytes(result[0].peak_bytes))
print(result[0].top("peak", heapwise.default_exclude(), 5))
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

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and roadmap.

## License

Apache License 2.0. See [LICENSE](LICENSE).
