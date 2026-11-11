"""The SerializableStep contract: what a subclass implements, and when it fails."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from adag import Pipeline, SerializableStep, Step


class Out(BaseModel):
    text: str


class Spec(BaseModel):
    greeting: str


@dataclass(frozen=True)
class SpecOnly(SerializableStep[Out]):
    """Defines only run: the default from_directory rebuilds it from spec."""

    kind = "test/spec-only/v1"
    spec: Spec

    def run(self, spec: Spec) -> Out:
        return Out(text=spec.greeting)


@dataclass(frozen=True)
class NeedsState(SerializableStep[Out]):
    """A required non-spec field and no from_directory: cannot be loaded back."""

    kind = "test/needs-state/v1"
    handle: str

    def run(self, handle: str) -> Out:
        return Out(text=handle)


def test_run_only_subclass_with_spec_round_trips(tmp_path: Path) -> None:
    pipeline = Pipeline(SpecOnly(spec=Spec(greeting="hi"))())
    pipeline.save(tmp_path)
    registry: dict[str, type[Step[Any]]] = {SpecOnly.kind: SpecOnly}
    reloaded = Pipeline.from_directory(tmp_path, registry)
    assert reloaded.execute().text == "hi"


def test_unloadable_subclass_fails_at_save_time(tmp_path: Path) -> None:
    pipeline = Pipeline(NeedsState(handle="h")())
    with pytest.raises(TypeError, match="handle.*Override from_directory"):
        pipeline.save(tmp_path)
    assert not tmp_path.joinpath("pipeline.json").exists()
