"""Every script in docs/examples/ runs as written, so the docs show working code."""

import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "docs" / "examples"
# Modules other examples import; they define a pipeline and run nothing on their own.
LIBRARY_FILES = {"cli_pipeline.py", "dispatch_pipeline.py"}


def _scripts() -> list[Path]:
    scripts = sorted(p for p in EXAMPLES_DIR.glob("*.py") if p.name not in LIBRARY_FILES)
    assert scripts, f"no example scripts under {EXAMPLES_DIR}"
    return scripts


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: p.name)
def test_example_runs(script: Path, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "out.svg")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_cli_example_runs(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "adag",
            "run",
            str(EXAMPLES_DIR / "cli_pipeline.py"),
            "--target",
            "total",
            "--param",
            "x=4",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "total: Number(value=17)" in result.stdout
