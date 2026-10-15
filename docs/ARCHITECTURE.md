# heapwise architecture

Working name: **heapwise** (free on PyPI as of 2026-10-15). Design follows the
research doc "Building the best Python memory profiler: do's and don'ts".

## Why Rust

Rust core, shipped as a normal wheel built with PyO3 + maturin, with a thin
Python layer for the CLI, attach and pytest.

What Rust buys us over C/C++ (what memray uses):

- **No system dependencies.** Native unwinding (`framehop`), DWARF/PDB
  symbolization (`gimli`, `addr2line`, `object`, `pdb`) and compression
  (`lz4_flex`, `zstd`) are pure Rust crates. memray's source builds need
  libunwind, liblz4 and libdebuginfod; we statically link everything.
- **Wheels for every OS from one toolchain.** `maturin` + `maturin-action`
  cross-build manylinux, musllinux, macOS (x86_64 + arm64) and Windows wheels.
  Windows is memray's biggest gap.
- **Safe code where most of the code is.** The allocation hook is a few hundred
  lines of `unsafe`; the much larger analysis side (replay, dominator trees,
  report and web UI server, pprof export) is ordinary safe, multi-threaded Rust.
- **Free-threaded CPython.** PyO3 supports 3.13t/3.14t; Rust's `Send`/`Sync`
  rules catch the data races a no-GIL build exposes.

Costs we accept:

- The hot path is still `unsafe` FFI. Rust does not make allocator hooking
  safer; the discipline below does.
- C/C++ has more prior art for native `malloc` interposition (memray's PLT
  patching, Microsoft Detours, MinHook). We will port those ideas, not reuse a
  library.
- `PyMem_SetAllocator` is not in the limited API, so wheels are per CPython
  version (no `abi3`). CI builds 3.12, 3.13, 3.14 and the free-threaded builds.
- Panics must never unwind into CPython. `extern "C"` hooks abort on panic,
  and the hook code avoids anything that can panic.

## Layers

```
python/heapwise/        thin Python layer
  cli.py                heapwise run script.py | -m module   (zero code changes)
  pytest_plugin.py      @pytest.mark.limit_memory("24 MB")
  __init__.py           profile() context manager, start/stop

src/                    Rust core (heapwise._native)
  hooks.rs              CPython allocator hooks, sampling, stack capture
  native/linux.rs       malloc/mmap interception via GOT patching (Linux)
  recorder.rs           cheap event log + interning on the hot path
  analysis.rs           peak / leaks / temporaries / timeline, after the run
  report.rs             findings-first terminal report
```

### Capture (hooks.rs)

- Wraps all three CPython allocator domains (RAW, MEM, OBJ) with
  `PyMem_SetAllocator`, like tracemalloc. OBJ is pymalloc, so small objects
  that never reach `malloc` are seen (do #7).
- Sampling: exponential gaps with a mean of 32 KiB by default, each sample
  weighted `size / (1 - e^(-size/rate))` (Go/tcmalloc). `--exact` records
  every allocation and counts temporaries.
- Stack capture walks Python frames with the public C API and records
  (code object, bytecode offset). Names and line numbers are resolved once at
  the end, not per allocation.
- Hook rules: never call Python while holding a lock; Python C-API calls only
  inside a thread-local re-entrancy guard; Rust's own allocations go to the
  system allocator, never through PyMem.

### Native allocations (native/)

- **Linux (x86_64, aarch64):** rewrite the GOT entries for `malloc`,
  `calloc`, `realloc`, `reallocarray`, `free`, `posix_memalign`,
  `aligned_alloc`, `memalign`, `valloc`, `pvalloc`, `mmap` (anonymous only)
  and `munmap` in every loaded C extension and the libraries it pulls in
  (OpenBLAS, Arrow, libstdc++ for `operator new`, ...). The wrappers call the
  real function through heapwise's own, unpatched GOT and report to the same
  recorder. No `LD_PRELOAD`, so it works when recording starts mid-run.
- **Never patched:** libc, the dynamic loader, the vDSO and heapwise itself.
  The interpreter (main executable or libpython) only gets `dlopen` patched:
  its allocations already arrive through the PyMem hooks, so patching its
  `malloc` would count them twice.
- **Libraries loaded later:** imports go through the interpreter's `dlopen`,
  which we hook, so the new extension is patched before its first call. Other
  loads (`ctypes.CDLL`, a library loading a plugin) are caught by checking the
  loader's load counter (`dlpi_adds`) every 1024 recorded allocations. We do
  not hook `dlopen` inside libraries because calling it from heapwise would
  change whose RUNPATH the loader searches.
- **Unpatching:** `stop()` restores each entry that still points at our
  wrapper, skipping libraries unloaded in the meantime. RELRO pages are made
  read-only again after each write.
- **Fork:** a `pthread_atfork` child handler turns recording off in the child,
  so a recorder lock held by another thread at fork time cannot deadlock it.
- **"Native" in reports** means memory requested by C code: these hooks plus
  the PyMem RAW domain. numpy 2.x allocates array data with
  `PyMem_RawMalloc`, so its buffers show up as native on every OS; the GOT
  hooks add libraries that call `malloc` directly.
- **Stacks:** a native allocation is credited to the Python line that called
  into C. Allocations with no Python stack (a library's own threads such as
  OpenBLAS workers, or calls that released the GIL) are grouped as
  `<no Python stack>`.

### Recording (recorder.rs)

heaptrack's model (do #9): each allocation/free appends a 24-byte event
(time, signed bytes, stack id). Frames and stacks are interned. Nothing is
aggregated on the hot path except the address → (bytes, stack) map needed to
attribute frees.

### Analysis (analysis.rs)

- **Peak**: pass 1 finds the high-water mark, pass 2 replays up to it to get
  what was live there.
- **Leaks**: still allocated when recording stopped.
- **Temporaries**: freed with no other allocation on the same thread in
  between (heaptrack's definition). Exact mode only.
- **Attribution**: each stack is credited to its innermost frame outside the
  stdlib, site-packages and heapwise itself (do #15).

### Output (report.rs)

Findings first, evidence second (do #10, #11):

```
Peak 53.3 MiB at 0.77 s. 98% of it was allocated by load_rows() at etl.py:7.
Still allocated at exit: 6.1 MiB, most of it from load_rows() at etl.py:7.
303070 temporary allocations (freed right after being made); churn() at etl.py:18 made 300001.
Memory over time  ▁▂▂▂▃▃▃▃▄▄▄▅▅▅▆▆▆▇▇▇███▇▆▅▄▃▂▂▂▂▂▂▂▂▂  peak 53.3 MiB
```

Process RSS is printed as context only, never attributed to lines (don't #1).

## Status of the scaffold

Working today (Linux, CPython 3.13, 20 tests passing):

- `heapwise run script.py` / `-m module`, exit code passed through
- Python allocator domains incl. pymalloc, all threads
- native `malloc`/`mmap` from C extensions on Linux, split out as "native"
  in the report
- sampling (default) and `--exact`
- Peak, Leaks, Temporaries views, timeline sparkline, findings-first summary
- `limit_memory` pytest marker
- CI builds wheels for Linux, musllinux, macOS and Windows and runs the tests

Measured on `examples/etl.py` (allocation-heavy micro benchmark, 0.12 s
plain): sampled mode 0.19 s, exact mode 1.7 s. Exact mode is over the 10x
budget for allocation-heavy code; see roadmap item 2.

## Roadmap

Ordered by what the research says matters most.

1. **Native allocations** (do #6). Done on Linux. Next: macOS (rebind
   `__la_symbol_ptr` / `__got` entries, fishhook-style) and Windows (IAT
   patching); native call stacks with `framehop` unwinding and `gimli`
   symbolization at analysis time, stitched under the Python frame that
   called them; attribute `<no Python stack>` memory to the library that
   allocated it; track partial `munmap`.
2. **Cheaper exact mode** (don't #6). Replace per-allocation frame walking
   with a shadow stack maintained from `sys.monitoring` PY_START/PY_RETURN
   events (3.12+) or the 3.14 unstable frame API; per-thread lock-free event
   buffers drained by a writer thread instead of one mutex.
3. **Stream to disk.** Events go to an lz4-compressed capture file as they
   happen, so hour-long runs do not keep the log in RAM. The same run still
   prints the summary (don't #5); the file feeds the UI and `diff`.
4. **Retained size and path to root** (do #12, the most requested gap). Heap
   snapshot via `gc.get_objects()` / `gc.get_referents()` from the Python
   layer, handed to Rust for a Lengauer-Tarjan dominator tree and shortest
   path from module globals, frames and thread stacks.
5. **Offline web UI** (do #14). `heapwise run` ends with "open report? [Y/n]";
   a single self-contained HTML file with flame graph, Peak / Leaks /
   Temporaries tabs, memory-over-time, retainers pane, collapsed non-user
   frames.
6. **Diff** (do #13). `heapwise diff before after` and a "mark" API
   (`heapwise.mark("after warmup")`) to bracket an action, plus a CI-friendly
   baseline comparison for the pytest plugin.
7. **Children and attach** (do #3, don't #8). Follow fork/spawn by default via
   an env var and a `.pth` hook shipped in the wheel. Attach by PID through
   PEP 768 `sys.remote_exec` on 3.14+, never gdb injection.
8. **Exports** (do #16). pprof `profile.proto`, speedscope JSON, collapsed
   stacks.
9. **Arena/fragmentation view** (do #7, don't #11). Report pymalloc arena
   retention separately so "RSS did not drop" is explained, not mistaken for a
   leak.

## Known limitations

- Requires CPython 3.12+ (deferred GC scheduling keeps frame walking inside
  the allocator safe).
- Allocations made without the GIL (RAW domain or native) have no Python
  stack yet; roadmap item 2's shadow stack fixes this.
- Native interception is Linux-only. Libraries that call `malloc` through a
  pointer obtained from `dlsym`, or use a statically linked allocator
  (jemalloc, mimalloc), are seen only through their `mmap` calls.
- A `realloc` that fails forgets the original block (rare, documented).
