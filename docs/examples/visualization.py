"""Visualization: render the graph to SVG, colored by cache state."""

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import InputParam, NullObserver, Pipeline, Step, dag


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class Square(Step[Number]):
    def run(self, x: int) -> Number:
        return Number(value=x * x)


@dataclass(frozen=True)
class AddOne(Step[Number]):
    def run(self, n: Number) -> Number:
        return Number(value=n.value + 1)


x = InputParam[int].with_default("x", 4)
total = AddOne()(n=Square()(x=x))
cache = Path(tempfile.mkdtemp())
Pipeline(total, cache_dir=cache).execute(observer=NullObserver())

out = Path(sys.argv[1]) if len(sys.argv) > 1 else cache / "graph.svg"
svg = dag(total, cache_dir=cache, path=out)
print(f"wrote {out.name}: {len(str(svg))} characters")
