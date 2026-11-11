"""In-flight dedup: concurrent resolution of one shared StepResult runs its body exactly once.

The per-execute memo only collapses resolutions that do not overlap in time. These tests drive
many threads at one shared node SIMULTANEOUSLY (a start barrier releases them together, and the
body sleeps to widen the memo-miss -> memo-write window), so without the ResolveContext in-flight
registry every racer would miss the memo and run the body. The dedup makes the body run once.
"""

import threading
import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from adag import FanOutStep, NullObserver, Pipeline, Step
from adag.deferred import ResolveContext


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
