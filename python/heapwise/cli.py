"""``heapwise run script.py`` — run a program and print where its memory went."""

from __future__ import annotations

import argparse
import os
import runpy
import sys

from . import DEFAULT_SAMPLE_RATE, default_exclude, format_bytes, parse_size, start, stop


def _peak_rss() -> int | None:
    """Process peak RSS, shown as context only, never as attribution."""
    try:
        import resource
    except ImportError:  # Windows
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def _run(args: argparse.Namespace) -> int:
    _launcher = os.path.abspath(sys.argv[0])
    if args.module:
        target, title = args.target, f"-m {args.target}"
        sys.argv = [target, *args.args]
    else:
        target = os.path.abspath(args.target)
        title = os.path.basename(target)
        sys.argv = [target, *args.args]
        sys.path.insert(0, os.path.dirname(target))

    # The launcher script (bin/heapwise or -m) is ours, not the user's.
    exclude = [*default_exclude(), _launcher]
    rate = 0 if args.exact else parse_size(args.rate)
    code = 0
    start(rate)
    try:
        if args.module:
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(target, run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:
        import traceback

        traceback.print_exc()
        code = 1
    finally:
        capture = stop()

    cwd = os.getcwd()
    report = capture.summary(title, exclude, cwd, args.top)
    notes = []
    rss = _peak_rss()
    if rss:
        notes.append(f"Process peak RSS {format_bytes(rss)} (includes the interpreter and memory heapwise cannot see yet).")
        report += f"\n{notes[-1]}\n"
    if args.html != "":
        from .html import write_html

        stem = args.target.replace(".", "_") if args.module else os.path.splitext(title)[0]
        path = args.html or f"heapwise-{stem}.html"
        try:
            write_html(capture, path, title, exclude, cwd, notes)
            report += f"\nFlame graph and call tree: {_link(os.path.abspath(path))}\n"
        except OSError as exc:
            report += f"\nCould not write the HTML report to {path}: {exc.strerror}\n"
    print("\n" + report, file=sys.stderr, end="")
    return code


def _link(path: str) -> str:
    from pathlib import Path

    return Path(path).as_uri()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="heapwise", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a Python script or module and report its memory use")
    run.add_argument("-m", dest="module", action="store_true", help="run a library module as a script")
    run.add_argument("--exact", action="store_true", help="record every allocation (slower; counts temporaries)")
    run.add_argument("--rate", default=str(DEFAULT_SAMPLE_RATE), help="mean bytes between samples (default 32KiB)")
    run.add_argument("--top", type=int, default=5, help="rows per view")
    run.add_argument(
        "--html",
        metavar="PATH",
        default=None,
        help="where to write the HTML report (default heapwise-<script>.html)",
    )
    run.add_argument("--no-html", dest="html", action="store_const", const="", help="skip the HTML report")
    run.add_argument("target", help="script path, or module name with -m")
    run.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed to the program")

    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
