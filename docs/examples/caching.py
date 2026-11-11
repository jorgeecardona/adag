"""Caching: what is in the key, and what Config leaves out."""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import Config, NullObserver, Pipeline, Step

RUNS: list[int] = []


class Total(BaseModel):
    value: int


@dataclass(frozen=True)
class Scale(Step[Total]):
    factor: int  # in the cache key
    workers: Config[int] = Config(4)  # not in the cache key

    def run(self, factor: int, workers: int) -> Total:
        RUNS.append(factor)
        return Total(value=10 * factor)


cache = Path(tempfile.mkdtemp())
for step in [
    Scale(factor=2),
    Scale(factor=2),
    Scale(factor=2, workers=Config(16)),
    Scale(factor=3),
]:
    before = len(RUNS)
    Pipeline(step(), cache_dir=cache).execute(observer=NullObserver())
    print(f"{step!r:45} ran: {len(RUNS) > before}")
