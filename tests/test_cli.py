"""Tests for the `adag run` console command."""

from pathlib import Path

import pytest

from adag import cli

PIPELINE_SOURCE = """
from dataclasses import dataclass

from pydantic import BaseModel

from adag.step import Step
from adag.deferred import InputParam


class Num(BaseModel):
    value: int


@dataclass(frozen=True)
class AddStep(Step[Num]):
    def run(self, base: int, bump: int) -> Num:
        return Num(value=base + bump)


base = InputParam[int]("base")
bump = InputParam.with_default("bump", 1)
total = AddStep()(base=base, bump=bump)
not_a_target = 42
"""


def _write_pipeline(tmp_path: Path) -> Path:
    path = tmp_path.joinpath("pipeline.py")
    path.write_text(PIPELINE_SOURCE)
    return path


def test_discover_targets_finds_step_results_only(tmp_path: Path) -> None:
    module = cli.load_pipeline_module(_write_pipeline(tmp_path))
    targets = cli.discover_targets(module)
    assert set(targets) == {"total"}


def test_run_pipeline_resolves_target_with_coerced_params(tmp_path: Path) -> None:
    path = _write_pipeline(tmp_path)
    results = cli.run_pipeline(
        path, ["total"], ["base=5", "bump=2"], cache_dir=tmp_path.joinpath("cache")
    )
    assert results["total"].value == 7


def test_run_pipeline_uses_param_default_when_omitted(tmp_path: Path) -> None:
    path = _write_pipeline(tmp_path)
    results = cli.run_pipeline(path, ["total"], ["base=10"], cache_dir=None)
    assert results["total"].value == 11  # bump defaults to 1


def test_run_pipeline_missing_required_param_errors(tmp_path: Path) -> None:
    path = _write_pipeline(tmp_path)
    with pytest.raises(ValueError):
        cli.run_pipeline(path, ["total"], [], cache_dir=None)


def test_run_pipeline_unknown_target_errors(tmp_path: Path) -> None:
    path = _write_pipeline(tmp_path)
    with pytest.raises(KeyError):
        cli.run_pipeline(path, ["missing"], ["base=1"], cache_dir=None)


def test_run_pipeline_unknown_param_errors(tmp_path: Path) -> None:
    path = _write_pipeline(tmp_path)
    with pytest.raises(ValueError):
        cli.run_pipeline(path, ["total"], ["base=1", "typo=9"], cache_dir=None)


def test_main_runs_and_prints_result(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write_pipeline(tmp_path)
    code = cli.main(
        ["run", str(path), "--target", "total", "--param", "base=5", "--param", "bump=2"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "total" in out and "7" in out


def test_main_tree_prints_execution_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_pipeline(tmp_path)
    code = cli.main(["run", str(path), "--target", "total", "--param", "base=5", "--tree"])
    out = capsys.readouterr().out
    assert code == 0
    assert "total" in out  # the result line still prints
    assert "AddStep" in out  # the execution tree names the resolved step


def test_main_help_without_target_lists_targets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_pipeline(tmp_path)
    code = cli.main(["run", str(path), "-h"])
    out = capsys.readouterr().out
    assert code == 0
    assert "total" in out


def test_main_help_with_target_lists_required_and_optional_params(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_pipeline(tmp_path)
    code = cli.main(["run", str(path), "--target", "total", "-h"])
    out = capsys.readouterr().out
    assert code == 0
    assert "base" in out and "bump" in out


STR_PARAM_SOURCE = """
from dataclasses import dataclass

from pydantic import BaseModel

from adag import InputParam, Step


class Text(BaseModel):
    value: str


@dataclass(frozen=True)
class Echo(Step[Text]):
    def run(self, code: str) -> Text:
        return Text(value=code)


out = Echo()(code=InputParam("code", value_type=str))
"""


def test_str_value_type_keeps_leading_zeros(tmp_path: Path) -> None:
    """A required str param declared with ``value_type`` is not guessed as an int."""
    path = tmp_path.joinpath("pipeline.py")
    path.write_text(STR_PARAM_SOURCE)
    results = cli.run_pipeline(path, ["out"], ["code=007"], cache_dir=None)
    assert results["out"].value == "007"


def test_help_shows_declared_value_type(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path.joinpath("pipeline.py")
    path.write_text(STR_PARAM_SOURCE)
    assert cli.main(["run", str(path), "--target", "out", "-h"]) == 0
    assert "<str>" in capsys.readouterr().out
