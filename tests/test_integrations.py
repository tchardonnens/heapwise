import re
import subprocess
import sys
import textwrap

pytest_plugins = ["pytester"]


def test_limit_memory_marker_fails_over_budget(pytester):
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.limit_memory("1 MB")
        def test_big():
            data = [bytearray(1024 * 1024) for _ in range(4)]

        @pytest.mark.limit_memory("10 MB")
        def test_small():
            data = [bytearray(1024 * 1024) for _ in range(4)]
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*over its 976.6 KiB limit*"])


def test_cli_runs_script_and_reports(tmp_path):
    script = tmp_path / "app.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys

            def build():
                return [bytearray(1 << 20) for _ in range(20)]

            data = build()
            sys.exit(3)
            """
        )
    )
    proc = subprocess.run(
        [sys.executable, "-m", "heapwise", "run", "--exact", str(script)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert proc.returncode == 3
    assert "build() at app.py:5" in proc.stderr
    # 20 MiB from build() plus whatever the interpreter needs to load the
    # script, which differs a little by platform.
    peak = re.search(r"Peak +([\d.]+) MiB", proc.stderr)
    assert peak and 20 <= float(peak.group(1)) < 22, proc.stderr
