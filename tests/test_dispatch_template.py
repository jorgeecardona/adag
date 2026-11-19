"""A template fan-out over an upstream node, dispatched to worker processes.

The worker imports the pipeline file, resolves the upstream node itself, and binds the template
to its own item. The main process computes the same branch hash from the same items, so the result
rejoins through the shared cache. Each test starts a few processes, so each one is kept small.
"""

import dataclasses
import os
from pathlib import Path
from typing import Any

import pytest

import adag.dispatch as dispatch_module
from adag import FanOutBranchError, Pipeline, StepResult
from adag.cli import load_pipeline_module, run_branch
from adag.dispatch import BranchReferenceError, SubprocessDispatcher

# A pipeline file. SALT changes the code of Measure, so it changes the branch hash. SPLIT is the
# upstream step: a plain one (its dict result is not cached) or one that saves its result.
PIPELINE_SOURCE = """
import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import FanOutStep, InputParam, Step, Template


class Size(BaseModel):
    chars: int
    pid: int


@dataclass(frozen=True)
class Measure(Step[Size]):
    text: str

    def run(self, text: str) -> Size:
        return Size(chars=len(text) + SALT, pid=os.getpid())


@dataclass(frozen=True)
class Split(Step[dict[str, str]]):
    marker: str

    def run(self, text: str, marker: str) -> dict[str, str]:
        with open(marker, "a") as handle:
            handle.write("x")
        return {part: part for part in text.split(";") if part}


@dataclass(frozen=True)
class SavedSplit(Split):
    def save_result(self, result, path: Path) -> None:
        path.write_text(json.dumps(result))

    def load_result(self, path: Path):
        return json.loads(path.read_text())


def measure(key, item):
    return Measure(text=item)()


text = InputParam[str]("text", value_type=str)
per_part = FanOutStep(
    template=Template(build=measure),
    pipeline_module=__file__,
    fan_out_var="per_part",
)(over=SPLIT(marker="MARKER")(text=text))
"""


@pytest.fixture(autouse=True)
def _worker_uses_checked_out_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker is a fresh interpreter: point it at this checkout, not at an installed release."""
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", f"{src_path}{os.pathsep}{existing}" if existing else src_path)


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """One entry per worker process that the dispatcher starts."""
    started: list[int] = []
    real_popen: Any = dispatch_module.subprocess.Popen

    def counting_popen(*args: Any, **kwargs: Any) -> Any:
        started.append(1)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(dispatch_module.subprocess, "Popen", counting_popen)
    return started


def _write(tmp_path: Path, *, salt: int = 0, split: str = "Split", name: str = "p") -> Path:
    path = tmp_path.joinpath(f"{name}.py")
    marker = tmp_path.joinpath(f"{name}.marker")
    path.write_text(
        PIPELINE_SOURCE.replace("SALT", str(salt))
        .replace("SPLIT", split)
        .replace("MARKER", marker.as_posix())
    )
    return path


def _runs_of_split(path: Path) -> int:
    marker = path.with_suffix(".marker")
    return len(marker.read_text()) if marker.exists() else 0


def test_branches_run_in_worker_processes_and_rejoin_by_hash(
    tmp_path: Path, spawns: list[int]
) -> None:
    path = _write(tmp_path)
    pipeline: Pipeline[Any] = Pipeline(
        load_pipeline_module(path).per_part, cache_dir=tmp_path.joinpath("cache")
    )

    result = pipeline.execute(dispatcher=SubprocessDispatcher(), text="ab;cde;f")

    assert {key: size.chars for key, size in result.items()} == {"ab": 2, "cde": 3, "f": 1}
    assert all(size.pid != os.getpid() for size in result.values())
    assert len(spawns) == 3  # one process per branch
    # The upstream result is a dict, so it is not cached: the main process ran it once and each
    # worker ran it again to find its item.
    assert _runs_of_split(path) == 1 + 3


def test_same_items_submit_no_job_and_a_changed_item_reruns_only_its_branch(
    tmp_path: Path, spawns: list[int]
) -> None:
    path = _write(tmp_path)
    pipeline: Pipeline[Any] = Pipeline(
        load_pipeline_module(path).per_part, cache_dir=tmp_path.joinpath("cache")
    )
    pipeline.execute(dispatcher=SubprocessDispatcher(), text="ab;cde;f")

    spawns.clear()
    again = pipeline.execute(dispatcher=SubprocessDispatcher(), text="ab;cde;f")
    assert set(again) == {"ab", "cde", "f"}
    assert spawns == []  # every branch is in the cache

    changed = pipeline.execute(dispatcher=SubprocessDispatcher(), text="ab;cde;gg")
    assert {key: size.chars for key, size in changed.items()} == {"ab": 2, "cde": 3, "gg": 2}
    assert len(spawns) == 1  # only the new item ran


def test_a_worker_reads_an_upstream_that_saves_its_result(tmp_path: Path) -> None:
    path = _write(tmp_path, split="SavedSplit")
    pipeline: Pipeline[Any] = Pipeline(
        load_pipeline_module(path).per_part, cache_dir=tmp_path.joinpath("cache")
    )

    result = pipeline.execute(dispatcher=SubprocessDispatcher(), text="ab;cde")

    assert set(result) == {"ab", "cde"}
    assert _runs_of_split(path) == 1  # the workers loaded it from the cache


def test_a_worker_with_different_code_fails_and_is_not_retried(
    tmp_path: Path, spawns: list[int]
) -> None:
    main = load_pipeline_module(_write(tmp_path, name="main"))
    other = _write(tmp_path, salt=1, name="other")  # Measure has other code, so other hashes
    target: StepResult[Any] = main.per_part
    step: Any = target.step
    pointed_at_other = StepResult(
        step=dataclasses.replace(step, pipeline_module=str(other)), sources=target.sources
    )

    pipeline: Pipeline[Any] = Pipeline(pointed_at_other, cache_dir=tmp_path.joinpath("cache"))
    with pytest.raises(FanOutBranchError) as caught:
        pipeline.execute(dispatcher=SubprocessDispatcher(), text="ab;cde")

    assert set(caught.value.failed) == {"ab", "cde"}
    assert all("hash mismatch" in detail for detail in caught.value.failed.values())
    assert len(spawns) == 2  # no retry


def test_run_branch_refuses_a_hash_that_is_not_its_own(tmp_path: Path) -> None:
    path = _write(tmp_path)
    with pytest.raises(BranchReferenceError, match="hash mismatch"):
        run_branch(
            path,
            "per_part",
            "ab",
            {"text": "ab;cde"},
            tmp_path.joinpath("cache"),
            expect_hash="0" * 12,
        )


def test_run_branch_names_the_keys_when_the_item_is_not_there(tmp_path: Path) -> None:
    path = _write(tmp_path)
    with pytest.raises(BranchReferenceError, match=r"not in fan-out.*'ab'"):
        run_branch(path, "per_part", "zz", {"text": "ab"}, tmp_path.joinpath("cache"))


def test_dispatch_needs_a_cache_dir_or_a_store(tmp_path: Path) -> None:
    pipeline: Pipeline[Any] = Pipeline(load_pipeline_module(_write(tmp_path)).per_part)
    with pytest.raises(ValueError, match="cache_dir or store"):
        pipeline.execute(dispatcher=SubprocessDispatcher(), text="ab")
