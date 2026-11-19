"""``runs_on`` picks where a step body runs, and one limit covers every step and branch."""

import asyncio
import os
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import pytest
from pydantic import BaseModel

from adag import (
    AdagDeprecationWarning,
    AsyncStep,
    FanOutStep,
    NullObserver,
    Pipeline,
    ResolveContext,
    Step,
    aresolve_all,
)
from adag.deferred import emit


class Pid(BaseModel):
    pid: int
    n: int = 0


class Count(BaseModel):
    n: int


@dataclass(frozen=True)
class InProcess(Step[Pid]):
    """Default placement: the body runs where the run runs."""

    def run(self) -> Pid:
        return Pid(pid=os.getpid())


@dataclass(frozen=True)
class OnThread(Step[Pid]):
    runs_on: ClassVar[Literal["loop", "thread", "process"]] = "thread"

    def run(self) -> Pid:
        return Pid(pid=threading.get_ident())


@dataclass(frozen=True)
class InChild(Step[Pid]):
    runs_on: ClassVar[Literal["loop", "thread", "process"]] = "process"
    n: int = 0

    def run(self, n: int = 0) -> Pid:
        return Pid(pid=os.getpid(), n=n)


class Events(NullObserver):
    def __init__(self) -> None:
        self.seen: list[str] = []

    def on_event(self, event: str, fields: dict[str, Any]) -> None:
        del fields
        self.seen.append(event)


@dataclass(frozen=True)
class Emits(Step[Count]):
    runs_on: ClassVar[Literal["loop", "thread", "process"]] = "process"

    def run(self) -> Count:
        emit("from.the.body")
        return Count(n=1)


@dataclass(frozen=True)
class EmitsOnThread(Step[Count]):
    runs_on: ClassVar[Literal["loop", "thread", "process"]] = "thread"

    def run(self) -> Count:
        emit("from.the.body")
        return Count(n=1)


_peak = {"now": 0, "max": 0}
_peak_lock = threading.Lock()


def _enter() -> None:
    with _peak_lock:
        _peak["now"] += 1
        _peak["max"] = max(_peak["max"], _peak["now"])


def _leave() -> None:
    with _peak_lock:
        _peak["now"] -= 1


@dataclass(frozen=True)
class Slow(Step[Count]):
    """A blocking body on a worker thread that records how many run at once."""

    n: int
    runs_on: ClassVar[Literal["loop", "thread", "process"]] = "thread"

    def run(self, n: int) -> Count:
        _enter()
        time.sleep(0.05)
        _leave()
        return Count(n=n)


@dataclass(frozen=True)
class SlowAsync(AsyncStep[Count]):
    """An awaiting body that records how many run at once."""

    n: int

    async def arun(self, n: int) -> Count:
        _enter()
        await asyncio.sleep(0.05)
        _leave()
        return Count(n=n)


def _reset_peak() -> None:
    _peak["now"] = 0
    _peak["max"] = 0


def test_a_step_runs_on_the_loop_by_default() -> None:
    assert Step.runs_on == "loop"
    result = asyncio.run(Pipeline(InProcess()()).aexecute(observer=NullObserver()))
    assert result.pid == os.getpid()


def test_runs_on_thread_runs_the_body_in_a_worker_thread() -> None:
    result = asyncio.run(Pipeline(OnThread()()).aexecute(observer=NullObserver()))
    assert result.pid != threading.get_ident()


def test_runs_on_thread_does_not_block_the_event_loop() -> None:
    ticks: list[int] = []

    async def ticker() -> None:
        while True:
            ticks.append(1)
            await asyncio.sleep(0.01)

    async def main() -> None:
        task = asyncio.create_task(ticker())
        context = ResolveContext(observer=NullObserver())
        await aresolve_all([Slow(n=1)(), Slow(n=2)()], context)
        task.cancel()

    asyncio.run(main())
    assert len(ticks) > 2


def test_runs_on_process_runs_the_body_in_another_process_on_the_async_path() -> None:
    result = asyncio.run(Pipeline(InChild(n=3)()).aexecute(observer=NullObserver()))
    assert result.pid != os.getpid()
    assert result.n == 3


def test_runs_on_process_runs_the_body_in_another_process_on_the_sync_path() -> None:
    result = Pipeline(InChild(n=4)()).execute(observer=NullObserver())
    assert result.pid != os.getpid()
    assert result.n == 4


def test_runs_on_process_result_is_cached_like_any_other(tmp_path: Any) -> None:
    first = Pipeline(InChild(n=5)(), cache_dir=tmp_path).execute(observer=NullObserver())
    second = Pipeline(InChild(n=5)(), cache_dir=tmp_path).execute(observer=NullObserver())
    assert second == first


def test_the_pool_ends_when_the_run_ends() -> None:
    context = ResolveContext(observer=NullObserver())
    context.process_pool()
    assert context._pools
    context.close()
    assert not context._pools
    context.close()  # a second call does nothing


def test_runs_on_process_refuses_a_step_that_cannot_be_pickled() -> None:
    @dataclass(frozen=True)
    class Local(Step[Count]):
        runs_on: ClassVar[Literal["loop", "thread", "process"]] = "process"

        def run(self) -> Count:
            return Count(n=1)

    with pytest.raises(TypeError, match=r"Local.*cannot be pickled"):
        Pipeline(Local()()).execute(observer=NullObserver())


def test_runs_on_process_refuses_a_step_that_overrides_execute() -> None:
    @dataclass(frozen=True)
    class NeedsContext(Step[Count]):
        runs_on: ClassVar[Literal["loop", "thread", "process"]] = "process"

        def run(self) -> Count:
            return Count(n=1)

        def execute(self, context: ResolveContext, /, **kwargs: Any) -> Count:
            del context, kwargs
            return Count(n=1)

    with pytest.raises(TypeError, match=r"NeedsContext.*overrides execute"):
        Pipeline(NeedsContext()()).execute(observer=NullObserver())


def test_runs_on_thread_refuses_an_async_step() -> None:
    @dataclass(frozen=True)
    class Awaits(AsyncStep[Count]):
        runs_on: ClassVar[Literal["loop", "thread", "process"]] = "thread"

        async def arun(self) -> Count:
            return Count(n=1)

    with pytest.raises(TypeError, match=r"Awaits.*no run\(\)"):
        asyncio.run(Pipeline(Awaits()()).aexecute(observer=NullObserver()))


def test_an_unknown_runs_on_value_is_refused() -> None:
    @dataclass(frozen=True)
    class Typo(Step[Count]):
        runs_on: ClassVar[Literal["loop", "thread", "process"]] = "proces"  # type: ignore[assignment]

        def run(self) -> Count:
            return Count(n=1)

    with pytest.raises(ValueError, match="Typo.runs_on"):
        asyncio.run(Pipeline(Typo()()).aexecute(observer=NullObserver()))


def test_an_observer_does_not_cross_into_a_process_but_does_into_a_thread() -> None:
    in_process = Events()
    asyncio.run(Pipeline(Emits()()).aexecute(observer=in_process))
    assert "from.the.body" not in in_process.seen

    on_thread = Events()
    asyncio.run(Pipeline(EmitsOnThread()()).aexecute(observer=on_thread))
    assert "from.the.body" in on_thread.seen


def test_max_concurrency_bounds_the_step_bodies_inside_fan_out_branches() -> None:
    _reset_peak()
    fan = FanOutStep(over={str(i): i for i in range(6)}, factory=lambda key, n: Slow(n=n)())()
    context = ResolveContext(observer=NullObserver())
    result = asyncio.run(aresolve_all([fan], context, max_concurrency=2))[0]
    assert {key: value.n for key, value in result.items()} == {str(i): i for i in range(6)}
    assert _peak["max"] == 2


def test_without_a_limit_the_branches_run_together() -> None:
    _reset_peak()
    fan = FanOutStep(over={str(i): i for i in range(6)}, factory=lambda key, n: Slow(n=n)())()
    context = ResolveContext(observer=NullObserver())
    asyncio.run(aresolve_all([fan], context))
    assert _peak["max"] > 2


def test_aexecute_runs_one_step_body_at_a_time_by_default() -> None:
    _reset_peak()
    fan = FanOutStep(over={str(i): i for i in range(4)}, factory=lambda key, n: Slow(n=n)())()
    asyncio.run(Pipeline(fan).aexecute(observer=NullObserver()))
    assert _peak["max"] == 1


def test_aexecute_takes_max_concurrency() -> None:
    _reset_peak()
    fan = FanOutStep(over={str(i): i for i in range(6)}, factory=lambda key, n: Slow(n=n)())()
    asyncio.run(Pipeline(fan).aexecute(observer=NullObserver(), max_concurrency=3))
    assert _peak["max"] == 3


def test_a_branch_can_be_an_async_step() -> None:
    _reset_peak()
    fan = FanOutStep(over={str(i): i for i in range(4)}, factory=lambda key, n: SlowAsync(n=n)())()
    result = asyncio.run(Pipeline(fan).aexecute(observer=NullObserver(), max_concurrency=4))
    assert {key: value.n for key, value in result.items()} == {str(i): i for i in range(4)}
    assert _peak["max"] == 4


def test_a_failing_branch_raises_as_it_is() -> None:
    @dataclass(frozen=True)
    class Boom(AsyncStep[Count]):
        n: int

        async def arun(self, n: int) -> Count:
            if n == 2:
                raise RuntimeError("branch two failed")
            await asyncio.sleep(0.01)
            return Count(n=n)

    fan = FanOutStep(over={str(i): i for i in range(4)}, factory=lambda key, n: Boom(n=n)())()
    with pytest.raises(RuntimeError, match="branch two failed"):
        asyncio.run(Pipeline(fan).aexecute(observer=NullObserver(), max_concurrency=4))


def test_fan_out_max_workers_is_deprecated() -> None:
    with pytest.warns(AdagDeprecationWarning, match=r"FanOutStep\(max_workers=\).*1\.0"):
        FanOutStep(over={"a": 1}, factory=lambda key, n: Slow(n=n)(), max_workers=2)


def test_wiring_a_fan_out_does_not_warn_a_second_time() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fan = FanOutStep(over={"a": 1}, factory=lambda key, n: Slow(n=n)(), max_workers=2)
        fan()
    assert [w.category for w in caught] == [AdagDeprecationWarning]


def test_execute_parallel_is_deprecated() -> None:
    with pytest.warns(AdagDeprecationWarning, match=r"parallel.*1\.0.*max_concurrency"):
        Pipeline(Slow(n=1)()).execute(observer=NullObserver(), parallel=True)
    with pytest.warns(AdagDeprecationWarning, match="max_workers"):
        Pipeline(Slow(n=1)()).execute(observer=NullObserver(), max_workers=2)


def test_execute_without_parallel_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", AdagDeprecationWarning)
        Pipeline(Slow(n=1)()).execute(observer=NullObserver())
