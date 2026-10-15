# heapwise

A Python memory profiler that tells you what is using your memory. Rust core,
`pip install`, no code changes.

```
$ heapwise run etl.py

heapwise · etl.py · 0.19 s · sampled, one per 32.0 KiB on average

Peak 52.1 MiB at 0.09 s. 99% of it was allocated by load_rows() at etl.py:7.
Still allocated at exit: 5.3 MiB, most of it from load_rows() at etl.py:7.
Memory over time  ▁▁▁▂▂▂▃▃▃▄▄▄▅▅▅▆▆▇▇▇███▇▆▅▄▃▂▂▂▂▂▂▂▂▂▂▂▂  peak 52.1 MiB

Peak: what was live at the high-water mark
   98.6%    51.4 MiB  load_rows  etl.py:7
    0.3%   157.4 KiB  remember   etl.py:12
...
```

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
