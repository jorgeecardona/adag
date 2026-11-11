"""Manifest round-trip: a pipeline rebuilt from disk must keep identical content hashes.

This is the load-bearing property for running the same graph across processes (and
eventually machines). A notebook restart already proves that a fresh process picks up
the on-disk cache — but only because it rebuilds the graph by re-running the same code.
A remote worker instead rebuilds the graph from the serialized manifest via
from_directory(). These tests verify that path produces byte-identical hashes, so a
worker computing a node lands its result under the key the orchestrator is waiting on.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from adag.deferred import InputParam
from adag.pipeline import Pipeline
from adag.serializable import SerializableStep
from adag.step import Step

CALLS: list[str] = []


class Greeting(BaseModel):
    """Carries a greeting line between steps."""

    text: str


class GreetSpec(BaseModel):
    """Configuration for the Greet step."""

    salutation: str


class ShoutSpec(BaseModel):
    """Configuration for the Shout step."""

    suffix: str = "!"


@dataclass(frozen=True)
class Greet(SerializableStep[Greeting]):
    """Builds a greeting from a runtime name and a configured salutation."""

    kind = "test/greet/v1"

    spec: GreetSpec

    def run(self, name: str, **_: Any) -> Greeting:
        CALLS.append("greet")
        return Greeting(text=f"{self.spec.salutation}, {name}")

    @classmethod
    def from_directory(cls, path: str) -> "Greet":
        return cls(spec=cls.load_spec(path, GreetSpec))


@dataclass(frozen=True)
class Shout(SerializableStep[Greeting]):
    """Uppercases an upstream greeting and appends a configured suffix."""

    kind = "test/shout/v1"

    spec: ShoutSpec

    def run(self, greeting: Greeting, **_: Any) -> Greeting:
        CALLS.append("shout")
        return Greeting(text=greeting.text.upper() + self.spec.suffix)

    @classmethod
    def from_directory(cls, path: str) -> "Shout":
        return cls(spec=cls.load_spec(path, ShoutSpec))


STEP_REGISTRY: dict[str, type[Step[Any]]] = {Greet.kind: Greet, Shout.kind: Shout}


def build_pipeline(cache_dir: Path | None = None) -> Pipeline:
    """Wire a two-step graph: name parameter -> Greet -> Shout."""
    name = InputParam[str]("name")
    greeting = Greet(spec=GreetSpec(salutation="Hello"))(name=name)
    shouted = Shout(spec=ShoutSpec(suffix="!!"))(greeting=greeting)
    return Pipeline(shouted, cache_dir=cache_dir)


def test_step_hashes_survive_manifest_roundtrip(tmp_path: Path):
    """Every step's content_hash is identical after save -> from_directory."""
    manifest_dir = tmp_path.joinpath("manifest")
    original = build_pipeline()
    original.save(manifest_dir)

    reloaded = Pipeline.from_directory(manifest_dir, STEP_REGISTRY)

    original_hashes = {type(step).__name__: step.content_hash() for step in original.steps}
    reloaded_hashes = {type(step).__name__: step.content_hash() for step in reloaded.steps}
    assert reloaded_hashes == original_hashes


def test_terminal_result_hash_survives_roundtrip(tmp_path: Path):
    """The terminal StepResult hash — the key a worker writes and an orchestrator polls — is preserved."""
    manifest_dir = tmp_path.joinpath("manifest")
    original = build_pipeline()
    original.save(manifest_dir)

    reloaded = Pipeline.from_directory(manifest_dir, STEP_REGISTRY)

    params = {"name": "world"}
    assert reloaded.output.content_hash(params=params) == original.output.content_hash(
        params=params
    )


def test_reconstructed_pipeline_reads_cache_written_by_original(tmp_path: Path):
    """A graph rebuilt purely from the manifest reuses the cache the original graph wrote.

    This is cache-as-inter-process-communication with the process boundary removed: if the
    hashes line up, the reconstructed pipeline serves every node from disk and recomputes nothing.
    """
    cache_dir = tmp_path.joinpath("cache")
    manifest_dir = tmp_path.joinpath("manifest")

    original = build_pipeline(cache_dir=cache_dir)
    CALLS.clear()
    first_result = original.execute(name="world")
    assert CALLS == ["greet", "shout"]
    original.save(manifest_dir)

    reloaded = Pipeline.from_directory(manifest_dir, STEP_REGISTRY, cache_dir=cache_dir)
    CALLS.clear()
    second_result = reloaded.execute(name="world")

    assert CALLS == []
    assert second_result.text == first_result.text
