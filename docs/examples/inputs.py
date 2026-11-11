"""Inputs: InputParam values and FileSource content are part of the cache key."""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import FileSource, InputParam, NullObserver, Pipeline, Step


class Count(BaseModel):
    words: int


@dataclass(frozen=True)
class CountWords(Step[Count]):
    def run(self, text: str, minimum: int) -> Count:
        return Count(words=sum(1 for word in text.split() if len(word) >= minimum))


with tempfile.TemporaryDirectory() as tmp:
    data = Path(tmp) / "notes.txt"
    data.write_text("a short note about caching")

    minimum = InputParam[int].with_default("minimum", 1)
    graph = CountWords()(text=FileSource(str(data)), minimum=minimum)
    pipeline = Pipeline(graph, cache_dir=Path(tmp) / "cache")

    print(pipeline.execute(observer=NullObserver()))
    print(pipeline.execute(observer=NullObserver(), minimum=5))
    data.write_text("the file changed, so the key changed too")
    print(pipeline.execute(observer=NullObserver()))
