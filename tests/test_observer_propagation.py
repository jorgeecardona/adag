"""current_observer() propagation across the async + thread boundaries the LLM steps use.

The base Step.execute binds the run's observer as the ambient current_observer() for the step body,
so step internals — including asyncio.to_thread workers and copy_context-wrapped ThreadPoolExecutor
workers — can emit DAG hooks (e.g. on_fan_out_branch) without threading an observer argument through
every signature. These tests mirror the a typical LLM fan-out shape (asyncio.run + to_thread + gather,
plus a rewrite pool). on_fan_out_branch is the hook used here.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from adag.deferred import NullObserver, current_observer
from adag.pipeline import Pipeline
from adag.step import Step


class Tally(BaseModel):
    """Minimal result model — how many worker calls fired."""

    calls: int


class RefusalRecorder(NullObserver):
    """Thread-safe collector of on_fan_out_branch emissions (mirrors the real observer's lock)."""

    def __init__(self) -> None:
        self.models: list[str] = []
        self._lock = threading.Lock()

    def on_fan_out_branch(self, key: str) -> None:
        with self._lock:
            self.models.append(key)


@dataclass(frozen=True)
class FanOutRefusals(Step[Tally]):
    """Emits on_fan_out_branch from asyncio.to_thread workers AND a copy_context ThreadPoolExecutor — the
    a typical LLM concurrency shape — via current_observer(), never a threaded-in argument.
    """

    runs: int

    def run(self, **_: Any) -> Tally:
        workers = asyncio.run(self._workers())
        rewrites = self._rewrites()
        return Tally(calls=workers + rewrites)

    async def _workers(self) -> int:
        counts = await asyncio.gather(
            *(asyncio.to_thread(self._one, "worker") for _ in range(self.runs))
        )
        return sum(counts)

    def _rewrites(self) -> int:
        with ThreadPoolExecutor(max_workers=self.runs) as pool:
            futures = [
                pool.submit(copy_context().run, self._one, "rewrite") for _ in range(self.runs)
            ]
            return sum(future.result() for future in futures)

    def _one(self, model: str) -> int:
        current_observer().on_fan_out_branch(model)
        return 1


def test_current_observer_propagates_through_to_thread_and_pool():
    """on_fan_out_branch fired from to_thread workers and a copy_context pool all reach the run's observer."""
    recorder = RefusalRecorder()
    result = Pipeline(FanOutRefusals(runs=3)()).execute(observer=recorder)

    assert result.calls == 6  # 3 workers + 3 rewrites
    assert sorted(recorder.models) == [
        "rewrite",
        "rewrite",
        "rewrite",
        "worker",
        "worker",
        "worker",
    ]


def test_current_observer_defaults_to_null_outside_a_run():
    """current_observer() outside any step run is a no-op NullObserver — emitting must not raise."""
    current_observer().on_fan_out_branch("model")
    assert isinstance(current_observer(), NullObserver)
