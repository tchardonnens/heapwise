"""A small program with a peak, a leak and lots of temporaries."""

_cache = []


def load_rows(n):
    return [{"id": i, "name": f"row-{i}", "payload": "x" * 200} for i in range(n)]


def remember(rows):
    # Keeps a slice alive until exit: shows up under Leaks.
    _cache.append(rows[: len(rows) // 10])


def churn(n):
    total = 0
    for i in range(n):
        total += len(str(i) * 3)  # short-lived strings: Temporaries
    return total


def main():
    rows = load_rows(200_000)
    remember(rows)
    del rows
    churn(300_000)


if __name__ == "__main__":
    main()
