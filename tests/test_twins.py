"""Two node objects of one step class with one cache key run once and share one result (#21).

The second node used to find the first one's result only in the result store. Without a store,
or with a result that is not saved, both ran.
"""

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from adag import (
    AsyncStep,
    Config,
    FanOutStep,
    NullObserver,
    Pipeline,
    ResolveContext,
    Step,
    aresolve_all,
)
from adag.cache_store import MemoryCacheStore

_runs: list[str] = []


class Handle:
    """A result with no result model: adag cannot save it."""


class Saved(BaseModel):
    value: int = 1


@dataclass(frozen=True)
class LoadHandle(Step[Handle]):
    n: int = 0

    def run(self, n: int) -> Handle:
        _runs.append("load")
        time.sleep(0.02)
        return Handle()


@dataclass(frozen=True)
class LoadSaved(Step[Saved]):
    n: int = 0

    def run(self, n: int) -> Saved:
        _runs.append("load")
        time.sleep(0.02)
        return Saved(value=n)


@dataclass(frozen=True)
class AsyncLoadHandle(AsyncStep[Handle]):
    async def arun(self) -> Handle:
        _runs.append("load")
        await asyncio.sleep(0.02)
        return Handle()


@dataclass(frozen=True)
class Pair(Step[Handle]):
    def run(self, a: object, b: object) -> Handle:
        return Handle()


@dataclass(frozen=True)
class Take(Step[Handle]):
    tag: int

    def run(self, tag: int, upstream: Handle) -> Handle:
        return upstream


@pytest.fixture(autouse=True)
def _clear_runs() -> None:
    _runs.clear()


@dataclass(frozen=True)
class Case:
    """One of the three ways the store can or cannot hold a result."""

    step: type[Step[Any]]
    stored: bool

    def store(self) -> MemoryCacheStore | None:
        return MemoryCacheStore() if self.stored else None


CASES = [
    pytest.param(Case(LoadHandle, stored=False), id="no-store"),
    pytest.param(Case(LoadHandle, stored=True), id="store-unsaved-result"),
    pytest.param(Case(LoadSaved, stored=True), id="store-saved-result"),
    pytest.param(Case(LoadSaved, stored=False), id="no-store-model"),
]


def _context(case: Case) -> ResolveContext:
    return ResolveContext(store=case.store(), observer=NullObserver())


def _sequential(case: Case) -> list[Any]:
    context = _context(case)
    return [case.step()().resolve(context), case.step()().resolve(context)]


def _threads(case: Case) -> list[Any]:
    context = _context(case)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(case.step()().resolve(context)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    return results


def _gathered(case: Case) -> list[Any]:
    async def main() -> list[Any]:
        context = _context(case)
        first, second = case.step()(), case.step()()
        return list(await asyncio.gather(first.aresolve(context), second.aresolve(context)))

    return asyncio.run(main())


def _all(case: Case) -> list[Any]:
    return asyncio.run(aresolve_all([case.step()(), case.step()()], _context(case)))


def _under_execute(case: Case) -> list[Any]:
    seen: list[Any] = []
    _run_pair(case, seen, lambda pipeline: pipeline.execute(observer=NullObserver()))
    return seen


def _under_aexecute(case: Case) -> list[Any]:
    seen: list[Any] = []
    _run_pair(
        case,
        seen,
        lambda pipeline: asyncio.run(
            pipeline.aexecute(observer=NullObserver(), max_concurrency=None)
        ),
    )
    return seen


def _run_pair(case: Case, seen: list[Any], run: Callable[[Pipeline[Any]], Any]) -> None:
    @dataclass(frozen=True)
    class Keep(Step[Handle]):
        def run(self, a: object, b: object) -> Handle:
            seen.extend([a, b])
            return Handle()

    run(Pipeline(Keep()(a=case.step()(), b=case.step()()), store=case.store()))


PATHS = [_sequential, _threads, _gathered, _all, _under_execute, _under_aexecute]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", CASES)
def test_twins_run_once_and_share_one_result(case: Case, path: Callable[[Case], list[Any]]) -> None:
    first, second = path(case)

    assert len(_runs) == 1
    assert first is second


@pytest.mark.parametrize("run_async", [False, True])
@pytest.mark.parametrize("stored", [False, True])
def test_fan_out_branches_that_build_equal_nodes_run_them_once(
    stored: bool, run_async: bool
) -> None:
    fan = FanOutStep(
        over={f"b{index}": index for index in range(4)},
        factory=lambda key, value: Take(tag=value)(upstream=LoadHandle()()),
    )()
    pipeline = Pipeline(fan, store=MemoryCacheStore() if stored else None)

    if run_async:
        results = asyncio.run(pipeline.aexecute(observer=NullObserver(), max_concurrency=None))
    else:
        results = pipeline.execute(observer=NullObserver())

    assert len(_runs) == 1
    assert len({id(result) for result in results.values()}) == 1


def test_nodes_with_a_different_field_run_separately() -> None:
    context = ResolveContext(observer=NullObserver())

    first = LoadHandle(n=1)().resolve(context)
    second = LoadHandle(n=2)().resolve(context)

    assert len(_runs) == 2
    assert first is not second


@dataclass(frozen=True)
class LoadOnDevice(Step[Handle]):
    device: Config[str]

    def run(self, device: str) -> Handle:
        _runs.append(device)
        return Handle()


def test_nodes_that_differ_only_in_a_config_field_share_one_result() -> None:
    context = ResolveContext(observer=NullObserver())

    first = LoadOnDevice(device=Config("cuda:0"))().resolve(context)
    second = LoadOnDevice(device=Config("cuda:1"))().resolve(context)

    assert _runs == ["cuda:0"]
    assert first is second


@dataclass(frozen=True)
class Unkeyed(Step[Handle]):
    client: object

    def run(self, client: object) -> Handle:
        _runs.append("unkeyed")
        return Handle()


def test_nodes_with_no_cache_key_run_separately() -> None:
    client = object()
    context = ResolveContext(observer=NullObserver())

    first = Unkeyed(client=client)().resolve(context)
    second = Unkeyed(client=client)().resolve(context)

    assert len(_runs) == 2
    assert first is not second


def _pinned_class(label: str) -> type[Step[Any]]:
    @dataclass(frozen=True)
    class Pinned(Step[str]):
        cache_key = "pinned"

        def run(self) -> str:
            return label

    return Pinned


@pytest.mark.parametrize("run_all", [False, True])
def test_two_step_classes_with_one_key_keep_their_own_results(run_all: bool) -> None:
    targets = [_pinned_class("first")()(), _pinned_class("second")()()]
    context = ResolveContext(observer=NullObserver())

    if run_all:
        results = asyncio.run(aresolve_all(targets, context))
    else:
        results = [target.resolve(context) for target in targets]

    assert results == ["first", "second"]


@dataclass(frozen=True)
class Effect(Step[None]):
    def run(self) -> None:
        _runs.append("effect")


def test_twins_that_return_none_run_once() -> None:
    context = ResolveContext(observer=NullObserver())

    Effect()().resolve(context)
    Effect()().resolve(context)

    assert _runs == ["effect"]


class _Events(NullObserver):
    def __init__(self) -> None:
        self.names: list[str] = []

    def on_step_start(self, step: str) -> None:
        self.names.append("start")

    def on_step_cached(self, step: str) -> None:
        self.names.append("cached")

    def on_step_finished(self, step: str, seconds: float) -> None:
        self.names.append("finished")


def test_the_second_twin_reports_a_cache_hit() -> None:
    events = _Events()
    context = ResolveContext(observer=events)

    LoadHandle()().resolve(context)
    LoadHandle()().resolve(context)

    assert events.names == ["start", "finished", "start", "cached"]


class _CountingStore(MemoryCacheStore):
    runs_on = "thread"

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def get(self, key: str) -> str | None:
        self.reads += 1
        return super().get(key)


def test_the_second_twin_does_not_read_a_thread_store() -> None:
    store = _CountingStore()
    context = ResolveContext(store=store, observer=NullObserver())

    async def main() -> tuple[Saved, Saved]:
        first = await LoadSaved()().aresolve(context)
        reads = store.reads
        second = await LoadSaved()().aresolve(context)
        assert store.reads == reads
        return first, second

    first, second = asyncio.run(main())

    assert first is second
    assert len(_runs) == 1


def test_a_waiting_twin_runs_the_body_when_the_first_is_cancelled() -> None:
    context = ResolveContext(observer=NullObserver())

    async def main() -> Handle:
        owner = asyncio.create_task(AsyncLoadHandle()().aresolve(context))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(AsyncLoadHandle()().aresolve(context))
        await asyncio.sleep(0)
        owner.cancel()
        return await waiter

    assert isinstance(asyncio.run(main()), Handle)
    assert len(_runs) == 2


def test_a_new_context_runs_the_body_again() -> None:
    LoadHandle()().resolve(ResolveContext(observer=NullObserver()))
    LoadHandle()().resolve(ResolveContext(observer=NullObserver()))

    assert len(_runs) == 2
