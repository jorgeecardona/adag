"""adag supports Python 3.12: the metadata says so and the code imports there."""

import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_requires_python_includes_312() -> None:
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    assert project["requires-python"] == ">=3.12"
    assert "Programming Language :: Python :: 3.12" in project["classifiers"]


def test_out_typevar_default_comes_from_typing_extensions() -> None:
    """``TypeVar(default=...)`` is 3.13+ in ``typing``; the backport keeps 3.12 importable."""
    import typing_extensions

    from adag.pipeline import Out

    assert isinstance(Out, typing_extensions.TypeVar)
    assert sys.version_info >= (3, 12)
