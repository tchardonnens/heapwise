"""Your code, a library (numpy) and the stdlib (json, collections) side by side."""

import json
from collections import Counter

import numpy as np


def load(n):
    return [{"id": i, "tags": ["a", "b", str(i % 7)]} for i in range(n)]


def features(rows):
    # numpy allocates this in C: shows up as "C code called in features".
    return np.random.default_rng(0).random((len(rows), 64))


def summarize(rows):
    return Counter(t for r in rows for t in r["tags"])


def export(rows):
    return json.dumps(rows)


def main():
    rows = load(150_000)
    matrix = features(rows)
    counts = summarize(rows)
    blob = export(rows[:60_000])
    return rows, matrix, counts, blob


if __name__ == "__main__":
    main()
