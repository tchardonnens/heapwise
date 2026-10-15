import ctypes
import shutil
import subprocess
import sys

import pytest

import heapwise

MiB = 1024 * 1024

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("cc") is None,
    reason="native interception is Linux-only for now; test needs a C compiler",
)

C_SOURCE = r"""
#include <stdlib.h>
#include <string.h>
void *grab(size_t n) { void *p = malloc(n); if (p) memset(p, 1, n); return p; }
void drop(void *p) { free(p); }
"""


def build_lib(tmp_path, name):
    src = tmp_path / f"{name}.c"
    src.write_text(C_SOURCE)
    out = tmp_path / f"lib{name}.so"
    subprocess.run(["cc", "-shared", "-fPIC", "-O1", "-o", str(out), str(src)], check=True)
    return out


def load(path):
    # PyDLL keeps the GIL during calls, so the Python stack is known.
    lib = ctypes.PyDLL(str(path))
    lib.grab.restype = ctypes.c_void_p
    lib.grab.argtypes = [ctypes.c_size_t]
    lib.drop.argtypes = [ctypes.c_void_p]
    return lib


def call_grab(lib, n):
    return lib.grab(n)


def test_malloc_in_a_library_loaded_before_start(tmp_path):
    lib = load(build_lib(tmp_path, "before"))
    with heapwise.profile() as result:
        p = call_grab(lib, 8 * MiB)
        lib.drop(p)
    cap = result[0]
    assert cap.native_supported
    assert cap.peak_native_bytes >= 8 * MiB
    func, file, _, nbytes, _, native = cap.top("peak", [], 1)[0]
    assert func == "call_grab" and file == __file__
    assert native >= 8 * MiB
    assert cap.leaked_bytes < 1 * MiB  # the free was seen


def test_library_loaded_after_start_is_patched(tmp_path):
    path = build_lib(tmp_path, "after")
    with heapwise.profile() as result:
        lib = load(path)
        [str(i) for i in range(5000)]  # enough allocations to trigger a rescan
        keep = call_grab(lib, 4 * MiB)
    assert result[0].leaked_bytes >= 4 * MiB
    lib.drop(keep)


def test_gil_released_allocations_are_counted(tmp_path):
    lib = ctypes.CDLL(str(build_lib(tmp_path, "nogil")))
    lib.grab.restype = ctypes.c_void_p
    lib.grab.argtypes = [ctypes.c_size_t]
    lib.drop.argtypes = [ctypes.c_void_p]
    with heapwise.profile() as result:
        p = lib.grab(6 * MiB)
        lib.drop(p)
    rows = result[0].top("peak", [], 3)
    assert any(r[0] == "<no Python stack>" and r[5] >= 6 * MiB for r in rows)


def test_hooks_are_removed_after_stop(tmp_path):
    lib = load(build_lib(tmp_path, "removed"))
    with heapwise.profile():
        pass
    # Allocating and freeing after stop must go straight to libc.
    lib.drop(call_grab(lib, 1 * MiB))
    with heapwise.profile() as result:
        pass
    assert result[0].peak_bytes < 1 * MiB


def test_numpy_buffers_count_as_native():
    np = pytest.importorskip("numpy")
    with heapwise.profile() as result:
        a = np.ones((1024, 1024))  # 8 MiB
        del a
    assert result[0].peak_native_bytes >= 8 * MiB


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_fork_while_recording_does_not_hang():
    import os
    import threading

    stop = threading.Event()

    def busy():
        while not stop.is_set():
            [bytearray(64) for _ in range(100)]

    with heapwise.profile():
        t = threading.Thread(target=busy)
        t.start()
        try:
            for _ in range(20):
                pid = os.fork()
                if pid == 0:
                    [bytearray(1024) for _ in range(1000)]
                    os._exit(0)
                _, status = os.waitpid(pid, 0)
                assert os.waitstatus_to_exitcode(status) == 0
        finally:
            stop.set()
            t.join()
