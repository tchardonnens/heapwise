"""numpy buffers are native memory: invisible to tracemalloc, seen by heapwise."""

import numpy as np

_kept = []


def load_matrix(n):
    return np.random.default_rng(0).random((n, n))  # n*n*8 bytes from malloc


def normalise(m):
    return (m - m.mean()) / m.std()  # temporaries the size of m


def main():
    m = load_matrix(2000)  # ~30.5 MiB
    z = normalise(m)
    _kept.append(z[:100].copy())  # ~1.5 MiB still alive at exit
    del m, z


if __name__ == "__main__":
    main()
