"""A BatchStep resolves on the async path, in a worker thread."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from adag import BatchItem, BatchStep, Pipeline, aresolve_all
from adag.deferred import ResolveContext

CALLS: list[tuple[int, int]] = []


class Num(BaseModel):
    n: int


@dataclass(frozen=True)
class Total(BatchStep[Num, Num]):
    def items_data(self, count: int) -> list[int]:
        return list(range(count))

    def process_item(self, item: BatchItem, count: int) -> Num:
        CALLS.append((item.data, threading.get_ident()))
        return Num(n=item.data)

    def assemble(self, results: list[Num], count: int) -> Num:
        return Num(n=sum(r.n for r in results))


@dataclass(frozen=True)
class OwnArun(Total):
    async def arun(self, *args: Any, **kwargs: Any) -> Num:
        del args, kwargs
        return Num(n=-1)


def test_aresolve_all_runs_the_items_off_the_loop_thread(tmp_path: Path) -> None:
    CALLS.clear()

    async def main() -> tuple[list[Any], int]:
        node = Total()(count=3)
        return await aresolve_all([node], ResolveContext(cache_dir=tmp_path)), threading.get_ident()

    results, loop_thread = asyncio.run(main())

    assert results == [Num(n=3)]
    assert [item for item, _ in CALLS] == [0, 1, 2]
    assert all(thread != loop_thread for _, thread in CALLS)


def test_aresolve_runs_a_batch_step(tmp_path: Path) -> None:
    async def main() -> Num:
        return await Total()(count=4).aresolve(ResolveContext(cache_dir=tmp_path))

    assert asyncio.run(main()) == Num(n=6)


def test_aexecute_reuses_item_results_saved_by_execute(tmp_path: Path) -> None:
    pipe = Pipeline(output=Total()(count=3), cache_dir=tmp_path)
    pipe.execute()
    for marker in tmp_path.glob("*.done"):
        marker.unlink()
    CALLS.clear()

    assert asyncio.run(pipe.aexecute()) == Num(n=3)
    assert CALLS == []


def test_a_subclass_arun_still_runs(tmp_path: Path) -> None:
    pipe = Pipeline(output=OwnArun()(count=3), cache_dir=tmp_path)

    assert asyncio.run(pipe.aexecute()) == Num(n=-1)
