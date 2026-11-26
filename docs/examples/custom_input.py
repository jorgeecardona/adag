"""An input of your own: content_hash puts its value in the cache key."""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import Deferred, NullObserver, Pipeline, ResolveContext, Step


@dataclass(frozen=True)
class EnvVar(Deferred[str]):
    name: str

    def resolve(self, context: ResolveContext) -> str:
        return os.environ[self.name]

    def content_hash(self) -> str | None:
        return os.environ.get(self.name)


class Greeting(BaseModel):
    text: str


@dataclass(frozen=True)
class Greet(Step[Greeting]):
    def run(self, name: str) -> Greeting:
        print(f"greeting {name}")
        return Greeting(text=f"hello {name}")


with tempfile.TemporaryDirectory() as tmp:
    pipeline = Pipeline(Greet()(name=EnvVar("ADAG_NAME")), cache_dir=Path(tmp))

    for name in ("ana", "ana", "bo"):
        os.environ["ADAG_NAME"] = name
        print(pipeline.execute(observer=NullObserver()).text)
