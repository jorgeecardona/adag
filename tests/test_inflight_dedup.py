"""In-flight dedup: concurrent resolution of one shared StepResult runs its body exactly once.

The per-execute memo only collapses resolutions that do not overlap in time. These tests drive
many threads at one shared node SIMULTANEOUSLY (a start barrier releases them together, and the
body sleeps to widen the memo-miss -> memo-write window), so without the ResolveContext in-flight
registry every racer would miss the memo and run the body. The dedup makes the body run once.
"""

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import AsyncStep, FanOutStep, NullObserver, Pipeline, Step
from adag.deferred import ResolveContext

# These tests cover max_workers and parallel, which are deprecated and removed in 1.0.
pytestmark = pytest.mark.filterwarnings("ignore::adag.AdagDeprecationWarning")


class Counted(BaseModel):
    value: int


@dataclass
class _Counter:
    """Thread-safe count of how many times a step body ran."""

    count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def increment(self) -> int:
        with self._lock:
            self.count += 1
            return self.count


@dataclass(frozen=True, eq=False)
class SlowCounting(Step[Counted]):
    """Increments the shared counter once per body run, after a sleep that widens the
    memo-miss -> memo-write window so a missing dedup lets every concurrent racer run.
    """

    counter: _Counter = field(compare=False)

    def run(self, counter: _Counter) -> Counted:
        time.sleep(0.05)
        return Counted(value=counter.increment())


@dataclass(frozen=True, eq=False)
class PassThrough(Step[Counted]):
    """A distinct branch node (eq=False -> identity) that just forwards its shared upstream."""

    def run(self, upstream: Counted) -> Counted:
        return upstream


def test_concurrent_resolve_of_one_stepresult_runs_body_once() -> None:
    """Eight threads resolving the same StepResult at once run its body once and share one instance."""
    counter = _Counter()
    shared = SlowCounting(counter=counter)()
    context = ResolveContext(observer=NullObserver())

    thread_count = 8
    start = threading.Barrier(thread_count)
    results: list[Counted] = []
    results_lock = threading.Lock()

    def worker() -> None:
        start.wait(timeout=3)
        resolved = shared.resolve(context)
        with results_lock:
            results.append(resolved)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert counter.count == 1
    assert len(results) == thread_count
    assert len({id(result) for result in results}) == 1


def test_shared_upstream_runs_once_under_concurrent_fan_out() -> None:
    """A shared upstream feeding many concurrent fan-out branches runs its body once, not per branch."""
    counter = _Counter()
    shared = SlowCounting(counter=counter)()
    branch_count = 8
    fan = FanOutStep(
        over={f"branch-{index}": index for index in range(branch_count)},
        factory=lambda key, value: PassThrough()(upstream=shared),
        max_workers=branch_count,
    )()

    results = Pipeline(fan).execute(observer=NullObserver())

    assert counter.count == 1
    assert len(results) == branch_count
    assert all(result.value == 1 for result in results.values())


_SHARED_RUNS = _Counter()


@dataclass(frozen=True)
class SlowShared(Step[Counted]):
    """An upstream whose nodes are equal by content: same class, same field, same key."""

    n: int

    def run(self, n: int) -> Counted:
        time.sleep(0.05)
        _SHARED_RUNS.increment()
        return Counted(value=n)


@dataclass(frozen=True)
class Tagged(Step[Counted]):
    """A branch step. The tag makes every branch key different."""

    tag: int

    def run(self, tag: int, upstream: Counted) -> Counted:
        return Counted(value=upstream.value + tag)


def test_equal_nodes_built_per_branch_run_the_body_once(tmp_path: Path) -> None:
    """Each branch builds its own upstream node. Equal keys mean the body runs once, not per node."""
    _SHARED_RUNS.count = 0
    branch_count = 8
    fan = FanOutStep(
        over={f"branch-{index}": index for index in range(branch_count)},
        factory=lambda key, value: Tagged(tag=value)(upstream=SlowShared(n=1)()),
        max_workers=branch_count,
    )()

    results = Pipeline(fan, cache_dir=tmp_path).execute(observer=NullObserver())

    assert _SHARED_RUNS.count == 1
    assert {key: result.value for key, result in results.items()} == {
        f"branch-{index}": 1 + index for index in range(branch_count)
    }


@dataclass(frozen=True)
class SlowSharedAsync(AsyncStep[Counted]):
    n: int

    async def arun(self, n: int) -> Counted:
        await asyncio.sleep(0.05)
        _SHARED_RUNS.increment()
        return Counted(value=n)


def test_equal_nodes_gathered_on_the_loop_run_the_body_once(tmp_path: Path) -> None:
    """Gathered tasks on two node objects with one key wait for one body run."""
    _SHARED_RUNS.count = 0
    context = ResolveContext(cache_dir=tmp_path, observer=NullObserver())

    async def main() -> tuple[Counted, Counted]:
        return await asyncio.gather(
            SlowSharedAsync(n=1)().aresolve(context), SlowSharedAsync(n=1)().aresolve(context)
        )

    first, second = asyncio.run(main())

    assert _SHARED_RUNS.count == 1
    assert first == second


@dataclass(frozen=True, eq=False)
class SlowFailing(Step[Counted]):
    counter: _Counter = field(compare=False)

    def run(self, counter: _Counter) -> Counted:
        time.sleep(0.05)
        counter.increment()
        raise ValueError("boom")


def test_failing_body_runs_once_per_waiter_and_every_waiter_sees_the_error() -> None:
    """A failed body releases its waiters, and each one runs the body itself (documented)."""
    counter = _Counter()
    shared = SlowFailing(counter=counter)()
    context = ResolveContext(observer=NullObserver())
    thread_count = 3
    start = threading.Barrier(thread_count)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker() -> None:
        start.wait(timeout=3)
        try:
            shared.resolve(context)
        except ValueError as error:
            with errors_lock:
                errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(errors) == thread_count
    assert counter.count == thread_count
