"""Async steps and parallel execution."""

import asyncio
import time
from dataclasses import dataclass

from pydantic import BaseModel

from adag import AsyncStep, NullObserver, Pipeline, Step


class Value(BaseModel):
    n: int


@dataclass(frozen=True)
class Fetch(AsyncStep[Value]):
    n: int

    async def arun(self, n: int) -> Value:
        await asyncio.sleep(0.1)  # stands in for an HTTP or database call
        return Value(n=n)


@dataclass(frozen=True)
class Sum(Step[Value]):
    def run(self, a: Value, b: Value) -> Value:
        return Value(n=a.n + b.n)


graph = Sum()(a=Fetch(n=1)(), b=Fetch(n=2)())
print("aexecute:", asyncio.run(Pipeline(graph).aexecute(observer=NullObserver())))


@dataclass(frozen=True)
class Slow(Step[Value]):
    n: int

    def run(self, n: int) -> Value:
        time.sleep(0.2)  # blocking work, for example a subprocess or a C extension
        return Value(n=n)


graph = Sum()(a=Slow(n=1)(), b=Slow(n=2)())
start = time.perf_counter()
result = Pipeline(graph).execute(observer=NullObserver(), parallel=True)
print(f"parallel: {result}, both Slow steps at once: {time.perf_counter() - start < 0.35}")
