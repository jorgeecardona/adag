"""Find which part of a cache key changed, and read the record adag saved with a result."""

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import InputParam, KeyExplanation, NullObserver, Pipeline, Step


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class Scale(Step[Number]):
    factor: int = 2

    def run(self, x: int, factor: int) -> Number:
        return Number(value=x * factor)


@dataclass(frozen=True)
class AddOne(Step[Number]):
    def run(self, n: Number) -> Number:
        return Number(value=n.value + 1)


def build(factor: int, cache: Path) -> Pipeline[Number]:
    x = InputParam[int]("x")
    return Pipeline(AddOne()(n=Scale(factor=factor)(x=x)), cache_dir=cache)


cache = Path(tempfile.mkdtemp())
params = {"x": 3}
old, new = build(2, cache), build(3, cache)

for before, after in zip(old.explain_keys(params), new.explain_keys(params), strict=True):
    for a, b in zip(before.components, after.components, strict=True):
        if a.digest != b.digest:
            print(f"{after.step}: {b.kind.value} {b.name} changed: {a.value} -> {b.value}")

old.execute(observer=NullObserver(), **params)
key = old.output.content_hash(params)
record = json.loads((cache / ".keys" / f"{key}.json").read_text())
saved = KeyExplanation.from_dict(record)
print(saved.step, "record has adag_version:", "adag_version" in record)
print("record equals explain_key:", saved == old.output.explain_key(params))
