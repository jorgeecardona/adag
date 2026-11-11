"""Tests for CachedStep direct save/load/is_cached API (non-Deferred path)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag.base import CachedStep


class Result(BaseModel):
    count: int


@dataclass(frozen=True)
class CountStep(CachedStep[Result]):
    """Trivial CachedStep that returns a constant — keeps the test focused on save/load."""

    value: int = 1

    def run(self, **_: object) -> Result:
        return Result(count=self.value)


def test_save_then_is_cached(tmp_path: Path):
    """save() writes both the output and the .done sentinel; is_cached() then returns True."""
    step = CountStep(value=3)
    assert step.is_cached(tmp_path) is False

    step.save(Result(count=3), tmp_path)
    assert step.is_cached(tmp_path) is True


def test_save_then_load_roundtrip(tmp_path: Path):
    """Saved result reloads with the same Pydantic shape and values."""
    step = CountStep(value=7)
    step.save(Result(count=7), tmp_path)

    loaded = step.load(tmp_path)
    assert loaded.count == 7


def test_is_partial_when_done_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """If the .done write fails after the output write, is_partial() reports True
    and is_cached() reports False — recoverable, not silently-cached.
    """
    from adag import io as dag_io

    step = CountStep(value=5)
    output = step.output_path(tmp_path)
    assert output is not None

    real_atomic_write_text = dag_io.atomic_write_text
    call_count = {"n": 0}

    def fail_second_write(path: Path, text: str) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated crash between output and done")
        real_atomic_write_text(path, text)

    monkeypatch.setattr("adag.base.atomic_write_text", fail_second_write)

    with pytest.raises(OSError, match="simulated crash"):
        step.save(Result(count=5), tmp_path)

    assert output.exists()
    assert step.is_partial(tmp_path) is True
    assert step.is_cached(tmp_path) is False


def test_save_leaves_no_temp_files(tmp_path: Path):
    """After a successful save, the cache directory only contains the output + .done sentinel."""
    step = CountStep(value=1)
    step.save(Result(count=1), tmp_path)

    files = sorted(p.name for p in tmp_path.iterdir())
    output_name = step.output_path(tmp_path)
    assert output_name is not None
    assert files == sorted([output_name.name, output_name.name + ".done"])
