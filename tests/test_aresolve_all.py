"""``aresolve_all``: resolve several targets level by level on the caller's loop."""

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import AsyncStep, NullObserver, Pipeline, ResolveContext, aresolve_all


class Num(BaseModel):
    value: int


CALLS: list[str] = []
RUNNING = {"now": 0, "peak": 0}


@dataclass(frozen=True)
class Source(AsyncStep[Num]):
    value: int = 1

    async def arun(self, value: int) -> Num:
        CALLS.append(f"source{value}")
        RUNNING["now"] += 1
        RUNNING["peak"] = max(RUNNING["peak"], RUNNING["now"])
        await asyncio.sleep(0.01)
        RUNNING["now"] -= 1
        return Num(value=value)


@dataclass(frozen=True)
class Add(AsyncStep[Num]):
    offset: int = 0

    async def arun(self, up: Num, offset: int) -> Num:
        CALLS.append(f"add{offset}")
        return Num(value=up.value + offset)


@dataclass(frozen=True)
class Boom(AsyncStep[Num]):
    async def arun(self) -> Num:
        await asyncio.sleep(0.001)
        raise KeyError("boom")


@dataclass(frozen=True)
class Slow(AsyncStep[Num]):
    async def arun(self) -> Num:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            CALLS.append("slow cancelled")
            raise
        return Num(value=0)


def _reset() -> None:
    CALLS.clear()
    RUNNING.update(now=0, peak=0)


def _context(**kwargs: object) -> ResolveContext:
    return ResolveContext(observer=NullObserver(), **kwargs)  # type: ignore[arg-type]


def test_results_follow_target_order_and_shared_node_runs_once() -> None:
    _reset()
    shared = Source(value=5)()
    targets = [Add(offset=2)(up=shared), Add(offset=1)(up=shared)]
    results = asyncio.run(aresolve_all(targets, _context()))
    assert [r.value for r in results] == [7, 6]
    assert CALLS.count("source5") == 1


def test_nodes_with_equal_keys_run_once() -> None:
    _reset()
    targets = [Source(value=3)(), Source(value=3)()]
    results = asyncio.run(aresolve_all(targets, _context()))
    assert [r.value for r in results] == [3, 3]
    assert CALLS == ["source3"]


def test_max_concurrency_bounds_running_bodies() -> None:
    _reset()
    targets = [Source(value=v)() for v in range(6)]
    asyncio.run(aresolve_all(targets, _context(), max_concurrency=2))
    assert RUNNING["peak"] == 2
    _reset()
    asyncio.run(aresolve_all(targets, _context()))
    assert RUNNING["peak"] == 6


def test_first_failure_cancels_the_level_and_keeps_its_type() -> None:
    _reset()
    with pytest.raises(KeyError, match="boom"):
        asyncio.run(aresolve_all([Slow()(), Boom()()], _context()))
    assert CALLS == ["slow cancelled"]


def test_cached_target_does_not_run_its_upstream(tmp_path: Path) -> None:
    _reset()
    target = Add(offset=1)(up=Source(value=2)())
    asyncio.run(aresolve_all([target], _context(cache_dir=tmp_path)))
    assert CALLS == ["source2", "add1"]
    _reset()
    fresh = Add(offset=1)(up=Source(value=2)())
    results = asyncio.run(aresolve_all([fresh], _context(cache_dir=tmp_path)))
    assert results[0].value == 3
    assert CALLS == []


def test_aexecute_still_resolves_the_output(tmp_path: Path) -> None:
    _reset()
    pipeline = Pipeline(Add(offset=4)(up=Source(value=1)()), cache_dir=tmp_path)
    assert asyncio.run(pipeline.aexecute(observer=NullObserver())).value == 5
