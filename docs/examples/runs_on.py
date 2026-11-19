"""runs_on: a CPU-bound step runs in a worker process, not on the event loop or a thread."""

import asyncio
import time
from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import BaseModel

from adag import FanOutStep, NullObserver, Pipeline, Step


class Total(BaseModel):
    primes: int


def count_primes(start: int, stop: int) -> int:
    """Count the primes in [start, stop) by trial division: CPU-bound work."""
    return sum(all(n % d for d in range(2, int(n**0.5) + 1)) for n in range(max(start, 2), stop))


@dataclass(frozen=True)
class CountOnThread(Step[Total]):
    start: int
    stop: int
    runs_on: ClassVar[Literal["loop", "thread", "process"]] = "thread"

    def run(self, start: int, stop: int) -> Total:
        return Total(primes=count_primes(start, stop))


@dataclass(frozen=True)
class CountInProcess(CountOnThread):
    runs_on: ClassVar[Literal["loop", "thread", "process"]] = "process"


def graph(step_class: type[CountOnThread]):
    spans = {str(i): (2_000_000 + i * 30_000, 2_000_000 + (i + 1) * 30_000) for i in range(4)}
    return FanOutStep(
        over=spans, factory=lambda key, span: step_class(start=span[0], stop=span[1])()
    )()


async def timed(step_class: type[CountOnThread]) -> tuple[float, int]:
    started = time.perf_counter()
    result = await Pipeline(graph(step_class)).aexecute(observer=NullObserver(), max_concurrency=4)
    return time.perf_counter() - started, sum(total.primes for total in result.values())


# A worker process imports this file again, so the guard keeps it from running the demo.
if __name__ == "__main__":
    for step_class in (CountOnThread, CountInProcess):
        seconds, primes = asyncio.run(timed(step_class))
        print(f"runs_on={step_class.runs_on!r:10} {primes} primes in {seconds:.2f} s")
