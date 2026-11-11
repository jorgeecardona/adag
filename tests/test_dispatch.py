"""Branch dispatch end to end: a worker resolves a branch, the orchestrator picks it up by hash.

The SubprocessDispatcher tests are the real proof of cache-as-IPC: branches resolve in
separate OS processes, write their results to a shared cache directory keyed by content
hash, and the orchestrator loads them back — no result ever crosses the wire, only the
branch reference.
"""

import os
from pathlib import Path
from typing import Any

import pytest

import adag.dispatch as dispatch_module
from adag.cli import load_pipeline_module
from adag.dispatch import (
    SubprocessDispatcher,
)
from adag.pipeline import Pipeline

# A self-contained pipeline file: a module-level FanOutStep over three keys, each branch a
# Double step whose result records the PID it ran in (to prove it ran in a worker process).
PIPELINE_SOURCE = """
import os
from dataclasses import dataclass

from pydantic import BaseModel

from adag.fan_out import FanOutStep
from adag.step import Step


class Doubled(BaseModel):
    key: str
    value: int
    pid: int


@dataclass(frozen=True)
class Double(Step[Doubled]):
    label_key: str
    n: int

    def run(self, label_key: str, n: int) -> Doubled:
        return Doubled(key=label_key, value=n * 2, pid=os.getpid())


def _branch(key, value):
    return Double(label_key=key, n=value)()


per_key = FanOutStep(
    over={"a": 1, "b": 2, "c": 3},
    factory=_branch,
    pipeline_module=__file__,
    fan_out_var="per_key",
)()
"""


def _write_pipeline(tmp_path: Path) -> Path:
    path = tmp_path.joinpath("pipeline.py")
    path.write_text(PIPELINE_SOURCE)
    return path


def test_branch_hash_is_stable_across_rederivation(tmp_path: Path):
    """Orchestrator and worker re-derive the same branch hash — the cache-as-IPC keystone."""
    path = _write_pipeline(tmp_path)
    orchestrator = load_pipeline_module(path)
    worker = load_pipeline_module(path)  # a fresh import, as the worker process would do

    orchestrator_fan: Any = orchestrator.per_key.step
    worker_fan: Any = worker.per_key.step
    orchestrator_hash = orchestrator_fan.factory("a", orchestrator_fan.over["a"]).content_hash()
    worker_hash = worker_fan.factory("a", worker_fan.over["a"]).content_hash()

    assert orchestrator_hash is not None
    assert orchestrator_hash == worker_hash


def test_subprocess_dispatcher_resolves_branches_in_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Each branch resolves in a child process; the orchestrator loads the results by hash."""
    # The dispatcher spawns `python -m adag resolve-branch` in a fresh
    # interpreter, which otherwise resolves adag from site-packages — a
    # possibly-stale install (e.g. the baked CI image). Point PYTHONPATH at the
    # checked-out source so the worker runs the same code as this test.
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", f"{src_path}{os.pathsep}{existing}" if existing else src_path)

    path = _write_pipeline(tmp_path)
    cache_dir = tmp_path.joinpath("cache")
    module = load_pipeline_module(path)

    spawns: list[int] = []
    real_run: Any = dispatch_module.subprocess.run

    def counting_run(*args: Any, **kwargs: Any) -> Any:
        spawns.append(1)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(dispatch_module.subprocess, "run", counting_run)

    pipeline: Pipeline[Any] = Pipeline(module.per_key, cache_dir=cache_dir)
    result = pipeline.execute(dispatcher=SubprocessDispatcher())

    assert {key: record.value for key, record in result.items()} == {"a": 2, "b": 4, "c": 6}
    # Every branch ran in a worker process, not the orchestrator.
    assert all(record.pid != os.getpid() for record in result.values())
    assert len(spawns) == 3  # one subprocess per branch

    # Second run: every branch is a cache hit, so nothing is spawned.
    spawns.clear()
    again = pipeline.execute(dispatcher=SubprocessDispatcher())
    assert {key: record.value for key, record in again.items()} == {"a": 2, "b": 4, "c": 6}
    assert len(spawns) == 0
