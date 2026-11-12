"""``aresolve``: gathered calls that share a node run its body once."""

import asyncio
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from adag import AsyncStep, NullObserver, ResolveContext


class Num(BaseModel):
    value: int


CALLS: list[str] = []


@dataclass(frozen=True)
class Shared(AsyncStep[Num]):
    async def arun(self) -> Num:
        CALLS.append("shared")
        await asyncio.sleep(0.01)
        return Num(value=1)


@dataclass(frozen=True)
class Flaky(AsyncStep[Num]):
    async def arun(self) -> Num:
        CALLS.append("flaky")
        await asyncio.sleep(0.01)
        if CALLS.count("flaky") == 1:
            raise RuntimeError("first attempt fails")
        return Num(value=2)


@dataclass(frozen=True)
class Use(AsyncStep[Num]):
    offset: int = 0

    async def arun(self, up: Num, offset: int) -> Num:
        return Num(value=up.value + offset)


def test_shared_upstream_runs_once_under_gather() -> None:
    CALLS.clear()
    shared = Shared()()
    a, b = Use(offset=1)(up=shared), Use(offset=2)(up=shared)
    context = ResolveContext(observer=NullObserver())

    async def main() -> list[Num]:
        return list(await asyncio.gather(a.aresolve(context), b.aresolve(context)))

    results = asyncio.run(main())
    assert [r.value for r in results] == [2, 3]
    assert CALLS == ["shared"]


def test_failed_owner_releases_the_waiter() -> None:
    CALLS.clear()
    flaky = Flaky()()
    a, b = Use(offset=1)(up=flaky), Use(offset=2)(up=flaky)
    context = ResolveContext(observer=NullObserver())

    async def main() -> list[object]:
        return list(
            await asyncio.gather(a.aresolve(context), b.aresolve(context), return_exceptions=True)
        )

    results = asyncio.run(main())
    assert isinstance(results[0], RuntimeError)
    assert isinstance(results[1], Num) and results[1].value == 4
    assert CALLS == ["flaky", "flaky"]


def test_waiter_cancellation_propagates() -> None:
    CALLS.clear()
    shared = Shared()()
    context = ResolveContext(observer=NullObserver())

    async def main() -> None:
        owner = asyncio.create_task(shared.aresolve(context))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(shared.aresolve(context))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (await owner).value == 1

    asyncio.run(main())
