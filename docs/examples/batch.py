"""BatchStep: each item is cached on its own, so a rerun resumes after a failure."""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import BatchItem, BatchStep, InputParam, NullObserver, Pipeline

CALLS: list[str] = []
FAIL_ON: set[str] = {"carol"}


class Greeting(BaseModel):
    text: str


class Greetings(BaseModel):
    lines: list[str]


@dataclass(frozen=True)
class Greet(BatchStep[Greetings, Greeting]):
    def items(self, names: list[str]) -> list[BatchItem]:
        return [BatchItem(key=name, data=name) for name in names]

    def process_item(self, item: BatchItem, names: list[str]) -> Greeting:
        CALLS.append(item.data)
        if item.data in FAIL_ON:
            raise RuntimeError(f"API error for {item.data}")
        return Greeting(text=f"hello {item.data}")

    def assemble(self, results: list[Greeting], names: list[str]) -> Greetings:
        return Greetings(lines=[result.text for result in results])


names = InputParam[list[str]]("names")
pipeline = Pipeline(Greet()(names=names), cache_dir=Path(tempfile.mkdtemp()))
everyone = ["alice", "bob", "carol", "dave"]

try:
    pipeline.execute(observer=NullObserver(), names=everyone)
except RuntimeError as error:
    print("first run failed:", error, "| called:", CALLS)

CALLS.clear()
FAIL_ON.clear()
result = pipeline.execute(observer=NullObserver(), names=everyone)
print("second run called:", CALLS)
print(result.lines)
