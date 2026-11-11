"""One SQLite file holds every step result and every batch item."""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import BatchItem, BatchStep, InputParam, NullObserver, Pipeline
from adag.cache_store import SqliteCacheStore


class Greeting(BaseModel):
    text: str


class Greetings(BaseModel):
    items: list[Greeting]


@dataclass(frozen=True)
class Greet(BatchStep[Greetings, Greeting]):
    items_source_keys = ("names",)

    def items_data(self, names: list[str]) -> list[str]:
        return names

    def process_item(self, item: BatchItem, names: list[str]) -> Greeting:
        print("greeting", item.data)
        return Greeting(text=f"hello {item.data}")

    def assemble(self, results: list[Greeting], names: list[str]) -> Greetings:
        return Greetings(items=results)


store = SqliteCacheStore(Path(tempfile.mkdtemp()) / "cache.db")
pipeline = Pipeline(Greet()(names=InputParam[list[str]]("names")), store=store)

pipeline.execute(observer=NullObserver(), names=["ana", "bo"])
pipeline.execute(observer=NullObserver(), names=["ana", "bo", "cy"])
print(len(list(store.iter_keys())), "keys in", store.path.name)
