"""Observers: watch a run. RunTreeObserver prints a timing tree with cache hits."""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import Pipeline, RunTreeObserver, Step


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class Load(Step[Number]):
    def run(self) -> Number:
        return Number(value=4)


@dataclass(frozen=True)
class Square(Step[Number]):
    def run(self, n: Number) -> Number:
        return Number(value=n.value**2)


graph = Square()(n=Load()())
cache = Path(tempfile.mkdtemp())

for label in ["first run", "second run"]:
    observer = RunTreeObserver()
    Pipeline(graph, cache_dir=cache).execute(observer=observer)
    print(f"{label}:\n{observer.render_tree()}")
