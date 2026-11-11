import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import InputParam, Pipeline, Step


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class Square(Step[Number]):
    def run(self, x: int) -> Number:
        print("computing square")
        return Number(value=x * x)


@dataclass(frozen=True)
class AddOne(Step[Number]):
    def run(self, n: Number) -> Number:
        return Number(value=n.value + 1)


x = InputParam[int]("x")
total = AddOne()(n=Square()(x=x))
pipeline = Pipeline(total, cache_dir=Path(tempfile.mkdtemp()))

print(pipeline.execute(x=4))  # computes both steps
print(pipeline.execute(x=4))  # served from the cache
