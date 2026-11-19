"""Concurrent FanOutStep: max_workers resolves branches in a thread pool, propagating ContextVars.

max_workers is None by default (sequential, back-compat, covered by the other fan-out tests). Set to
an int it resolves branches concurrently; these tests prove the branches actually overlap, that ambient
ContextVars reach every worker, that the results dict is correct, and that a branch failure propagates.
"""

import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import FanOutStep, NullObserver, Pipeline, Step

# These tests cover max_workers and parallel, which are deprecated and removed in 1.0.
pytestmark = pytest.mark.filterwarnings("ignore::adag.AdagDeprecationWarning")

_MARKER: ContextVar[str] = ContextVar("fan_out_marker", default="unset")


class Marker(BaseModel):
    value: str


class Doubled(BaseModel):
    value: int


class BranchBoomError(RuntimeError):
    """Raised by a branch step to prove a branch failure propagates out of the concurrent fan-out."""


@dataclass(frozen=True, eq=False)
class BarrierBranch(Step[Marker]):
    """Waits on a shared barrier: every branch must be in-flight at once or the wait times out,
    so the test hangs/raises if the branches were resolved sequentially.
    """

    barrier: threading.Barrier = field(compare=False)

    def run(self, barrier: threading.Barrier) -> Marker:
        barrier.wait(timeout=3)
        return Marker(value="passed")


@dataclass(frozen=True, eq=False)
class ReadMarker(Step[Marker]):
    """Reads the ambient ContextVar inside its (worker-thread) body."""

    def run(self) -> Marker:
        return Marker(value=_MARKER.get())


@dataclass(frozen=True, eq=False)
class Doubler(Step[Doubled]):
    n: int

    def run(self, n: int) -> Doubled:
        return Doubled(value=n * 2)


@dataclass(frozen=True, eq=False)
class Boom(Step[Doubled]):
    def run(self) -> Doubled:
        raise BranchBoomError("branch failed")


def test_branches_run_concurrently_with_max_workers() -> None:
    branch_count = 4
    barrier = threading.Barrier(branch_count)
    fan = FanOutStep(
        over={f"b{index}": index for index in range(branch_count)},
        factory=lambda key, value: BarrierBranch(barrier=barrier)(),
        max_workers=branch_count,
    )()

    results = Pipeline(fan).execute(observer=NullObserver())

    assert set(results.keys()) == {f"b{index}" for index in range(branch_count)}
    assert all(result.value == "passed" for result in results.values())


def test_context_var_propagates_into_every_branch_worker() -> None:
    fan = FanOutStep(
        over={f"b{index}": index for index in range(4)},
        factory=lambda key, value: ReadMarker()(),
        max_workers=4,
    )()

    token = _MARKER.set("bound")
    try:
        results = Pipeline(fan).execute(observer=NullObserver())
    finally:
        _MARKER.reset(token)

    assert {result.value for result in results.values()} == {"bound"}


def test_concurrent_results_dict_is_correct() -> None:
    fan = FanOutStep(
        over={"a": 1, "b": 2, "c": 3},
        factory=lambda key, value: Doubler(n=value)(),
        max_workers=3,
    )()

    results = Pipeline(fan).execute(observer=NullObserver())

    assert {key: result.value for key, result in results.items()} == {"a": 2, "b": 4, "c": 6}


def test_branch_failure_propagates() -> None:
    fan = FanOutStep(
        over={"ok": 1, "boom": 2},
        factory=lambda key, value: Boom()() if key == "boom" else Doubler(n=value)(),
        max_workers=2,
    )()

    with pytest.raises(BranchBoomError):
        Pipeline(fan).execute(observer=NullObserver())


def test_threaded_results_keep_the_order_of_over() -> None:
    """Results come back in ``over`` order even when later branches finish first."""
    import time as _time

    @dataclass(frozen=True, eq=False)
    class SleepThenEcho(Step[Doubled]):
        n: int

        def run(self, n: int) -> Doubled:
            _time.sleep(0.05 * (5 - n))  # the first key finishes last
            return Doubled(value=n)

    fan = FanOutStep(
        over={f"k{index}": index for index in range(5)},
        factory=lambda key, value: SleepThenEcho(n=value)(),
        max_workers=5,
    )()
    results = Pipeline(fan).execute(observer=NullObserver())
    assert list(results) == [f"k{index}" for index in range(5)]


def test_fan_out_resolves_under_aexecute(tmp_path: Path):
    """A fan-out runs on the async path instead of raising NotImplementedError."""
    import asyncio

    class Twice(BaseModel):
        v: int

    @dataclass(frozen=True)
    class Double(Step[Twice]):
        n: int

        def run(self, n: int) -> Twice:
            return Twice(v=n * 2)

    fan = FanOutStep(over={"a": 1, "b": 2}, factory=lambda key, n: Double(n=n)())()
    result = asyncio.run(Pipeline(fan, cache_dir=tmp_path / "cache").aexecute())
    assert {key: r.v for key, r in result.items()} == {"a": 2, "b": 4}
