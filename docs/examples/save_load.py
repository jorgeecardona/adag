"""Save a pipeline to a directory and load it back with the same cache keys."""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from adag import InputParam, NullObserver, Pipeline, SerializableStep


class Greeting(BaseModel):
    text: str


class GreetSpec(BaseModel):
    salutation: str


@dataclass(frozen=True)
class Greet(SerializableStep[Greeting]):
    kind = "example/greet/v1"  # the registry name written to step.json
    spec: GreetSpec

    def run(self, name: str, **_: Any) -> Greeting:
        return Greeting(text=f"{self.spec.salutation}, {name}")

    @classmethod
    def from_directory(cls, path: str) -> "Greet":
        return cls(spec=cls.load_spec(path, GreetSpec))


name = InputParam[str]("name")
pipeline = Pipeline(Greet(spec=GreetSpec(salutation="Hello"))(name=name))

folder = Path(tempfile.mkdtemp()) / "greeter"
pipeline.save(folder)
print(sorted(path.name for path in folder.rglob("*.json")))

loaded = Pipeline.from_directory(folder, step_registry={Greet.kind: Greet})
print(loaded.execute(observer=NullObserver(), name="Ada"))
print("same key:", loaded.output.content_hash() == pipeline.output.content_hash())
