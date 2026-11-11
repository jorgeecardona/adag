"""Tests for Pipeline.execute(parallel=True) — level-concurrent execution."""

import threading
from dataclasses import dataclass, field

from pydantic import BaseModel

from adag import Pipeline, RunTreeObserver, Step


class Result(BaseModel):
    count: int


@dataclass(frozen=True)
class Source(Step[Result]):
    def run(self) -> Result:
        return Result(count=1)


@dataclass(frozen=True)
class Increment(Step[Result]):
    bump: int = 1  # distinct values -> distinct nodes; passed to run() as a kwarg

    def run(self, prev: Result, bump: int) -> Result:
        return Result(count=prev.count + bump)


@dataclass(frozen=True)
class Join(Step[Result]):
    def run(self, left: Result, right: Result) -> Result:
        return Result(count=left.count + right.count)


@dataclass(frozen=True)
class BarrierStep(Step[Result]):
    bump: int
    barrier: threading.Barrier = field(compare=False)

    def run(self, prev: Result, bump: int, barrier: threading.Barrier) -> Result:
        # Both branch steps must be in-flight at once or this times out — proves concurrency.
        barrier.wait(timeout=3)
        return Result(count=prev.count + bump)


def _diamond() -> Pipeline[Result]:
    source = Source()()
    left = Increment(bump=10)(prev=source)
    right = Increment(bump=20)(prev=source)
    return Pipeline(Join()(left=left, right=right))


def test_parallel_result_matches_sequential():
    sequential = _diamond().execute()
    parallel = _diamond().execute(parallel=True)
    assert parallel.count == sequential.count == 32


def test_parallel_runs_independent_steps_concurrently():
    barrier = threading.Barrier(2)
    source = Source()()
    left = BarrierStep(bump=1, barrier=barrier)(prev=source)
    right = BarrierStep(bump=2, barrier=barrier)(prev=source)
    pipeline = Pipeline(Join()(left=left, right=right))

    # Sequential execution would block the first .wait() until it times out and raises;
    # reaching count==5 (1+1 + 1+2 = 2+3) proves both branch steps ran at the same time.
    result = pipeline.execute(parallel=True)
    assert result.count == 5


def test_run_tree_observer_flat_under_parallel():
    observer = RunTreeObserver()
    _diamond().execute(parallel=True, observer=observer)
    tree = observer.render_tree()
    assert len(tree.splitlines()) == 4  # Source, two Increments, Join — level-scheduled, no nesting
    assert "└" not in tree and "├" not in tree


def test_run_tree_observer_nests_when_sequential():
    observer = RunTreeObserver()
    _diamond().execute(observer=observer)
    tree = observer.render_tree()
    assert "└" in tree or "├" in tree  # recursive resolve nests children — unchanged
