import json
import subprocess
import sys
import textwrap

import heapwise
from heapwise.html import report_data


def outer():
    return inner()


def inner():
    return [bytearray(1 << 20) for _ in range(4)]


def _page_data(path):
    text = path.read_text(encoding="utf-8")
    raw = text.split('<script type="application/json" id="data">', 1)[1].split("</script>", 1)[0]
    return json.loads(raw.replace("<\\/", "</"))


def test_stacks_are_outermost_first_and_cover_the_peak():
    with heapwise.profile() as result:
        keep = outer()
    cap = result[0]
    frames = cap.frames()
    stacks = cap.stacks("peak")
    assert sum(b for _, b, _, _ in stacks) == cap.peak_bytes
    path = stacks[0][0]
    names = [frames[i][0] for i in path]
    assert names[-1].endswith("inner")
    assert names.index(next(n for n in names if n.endswith("outer"))) < len(names) - 1
    del keep


def test_report_data_merges_frames_on_one_line():
    with heapwise.profile() as result:
        keep = outer()
    data = report_data(result[0], "t")
    keys = [(f, file, line) for f, file, line, _ in data["frames"]]
    assert len(keys) == len(set(keys))
    assert data["views"]["temporaries"] is not None  # exact mode
    del keep


def test_write_html_is_self_contained(tmp_path):
    with heapwise.profile() as result:
        keep = outer()
    out = heapwise.write_html(result[0], tmp_path / "r.html", "demo", heapwise.default_exclude())
    text = (tmp_path / "r.html").read_text(encoding="utf-8")
    assert out.endswith("r.html")
    assert "<script src" not in text and "<link" not in text
    data = _page_data(tmp_path / "r.html")
    assert data["peak"] == result[0].peak_bytes
    assert any(f[0].endswith("inner") and f[3] for f in data["frames"])
    del keep


def test_html_flag_sets_the_report_path(tmp_path):
    script = tmp_path / "job.py"
    script.write_text("def f():\n    return [bytearray(1 << 20) for _ in range(8)]\nx = f()\n")
    proc = subprocess.run(
        [sys.executable, "-m", "heapwise", "run", "--exact", "--html", "out.html", str(script)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "out.html" in proc.stderr
    data = _page_data(tmp_path / "out.html")
    assert data["title"] == "job.py"
    assert data["peak"] >= 8 << 20
    func, path, line, size, count, native, kind, pkg = data["top"]["peak"]
    assert (func, path, kind) == ("f", "job.py", "mine") and size >= 8 << 20
    assert data["allocations"] >= 8 and data["allocated"] >= size


def test_cli_writes_report_by_default_and_skips_with_flag(tmp_path):
    script = tmp_path / "app.py"
    script.write_text(textwrap.dedent("data = [bytearray(1 << 20) for _ in range(3)]\n"))
    run = lambda *a: subprocess.run(
        [sys.executable, "-m", "heapwise", "run", *a, str(script)], capture_output=True, text=True, cwd=tmp_path
    )
    run("--no-html")
    assert not (tmp_path / "heapwise-app.html").exists()
    proc = run()
    assert (tmp_path / "heapwise-app.html").exists()
    assert "Flame graph and call tree: file://" in proc.stderr
    # Sampled runs don't count temporaries, so that tab is off.
    assert _page_data(tmp_path / "heapwise-app.html")["views"]["temporaries"] is None


def test_startup_allocations_get_one_label(tmp_path):
    script = tmp_path / "s.py"
    script.write_text("x = 1\n" * 2000 + "data = bytearray(1 << 20)\n")
    proc = subprocess.run(
        [sys.executable, "-m", "heapwise", "run", "--exact", "--no-html", "--top", "20", str(script)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert "<frozen runpy>" not in proc.stderr
    assert "<script startup>" in proc.stderr


def test_files_are_sorted_into_mine_libraries_and_stdlib(tmp_path):
    import os
    import sysconfig

    from heapwise.html import _Classifier

    c = _Classifier(heapwise.default_exclude())
    std = sysconfig.get_paths()["stdlib"]
    site = os.path.join(str(tmp_path), "venv", "lib", "site-packages")
    assert c(os.path.join(site, "numpy", "core", "numeric.py"), None) == (
        os.path.join("numpy", "core", "numeric.py"), "lib", "numpy"
    )
    assert c(os.path.join(site, "six.py"), None)[1:] == ("lib", "six")
    assert c(os.path.join(std, "json", "encoder.py"), None)[1:] == ("std", "json")
    assert c("<frozen importlib._bootstrap>", None)[1:] == ("std", "importlib")
    assert c(heapwise.__file__, None)[1:] == ("tool", "heapwise")
    app = os.path.join(str(tmp_path), "proj", "app.py")
    assert c(app, os.path.join(str(tmp_path), "proj")) == ("app.py", "mine", "")


def test_page_marks_library_frames():
    import json as json_mod

    def dump():
        return [json_mod.dumps({"k": list(range(50))}) for _ in range(2000)]

    with heapwise.profile() as result:
        keep = dump()
    data = report_data(result[0], "t", heapwise.default_exclude())
    kinds = {(kind, pkg) for _, kind, pkg in data["files"]}
    assert ("std", "json") in kinds
    assert ("mine", "") in kinds
    assert data["max_depth"] == heapwise._native.MAX_STACK_DEPTH
    del keep
