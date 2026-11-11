"""Tests for Parameter — runtime values in the DAG."""

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import InputParam, Pipeline, Step


class CountResult(BaseModel):
    """Simple result."""

    count: int


class GreetResult(BaseModel):
    """Result with text."""

    greeting: str


@dataclass(frozen=True)
class GreetStep(Step[GreetResult]):
    """Greets the given name."""

    prefix: str = "Hello"

    def run(self, name: str, prefix: str) -> GreetResult:
        return GreetResult(greeting=f"{prefix} {name}")


@dataclass(frozen=True)
class LengthStep(Step[CountResult]):
    """Counts the length of text."""

    def run(self, text: str) -> CountResult:
        return CountResult(count=len(text))


@dataclass(frozen=True)
class ThresholdStep(Step[CountResult]):
    """Compares value against threshold."""

    value: int = 10

    def run(self, value: int, threshold: float) -> CountResult:
        return CountResult(count=1 if value > threshold else 0)


def test_parameter_resolved_at_execution(tmp_path: Path):
    """Parameter is filled from execute kwargs."""
    name = InputParam[str]("name")
    step = GreetStep()
    result = step(name=name)
    pipeline = Pipeline(output=result, cache_dir=tmp_path)

    output = pipeline.execute(name="World")
    assert output.greeting == "Hello World"


def test_parameter_with_default(tmp_path: Path):
    """Parameter with default doesn't need to be provided."""
    threshold = InputParam[float].with_default("threshold", 5.0)
    step = ThresholdStep(value=10)
    result = step(threshold=threshold)
    pipeline = Pipeline(output=result, cache_dir=tmp_path)

    output = pipeline.execute()
    assert output.count == 1


def test_parameter_override_default(tmp_path: Path):
    """Provided value overrides the default."""
    threshold = InputParam[float].with_default("threshold", 5.0)
    step = ThresholdStep(value=3)
    result = step(threshold=threshold)
    pipeline = Pipeline(output=result, cache_dir=tmp_path)

    output = pipeline.execute(threshold=1.0)
    assert output.count == 1


def test_parameter_missing_raises(tmp_path: Path):
    """Missing required parameter raises ValueError."""
    name = InputParam[str]("name")
    step = GreetStep()
    result = step(name=name)
    pipeline = Pipeline(output=result, cache_dir=tmp_path)

    with pytest.raises(ValueError, match="Missing parameters"):
        pipeline.execute()


def test_input_names_exposes_parameter_set(tmp_path: Path):
    """Pipeline.input_names returns the set of InputParam names — public alternative to _inputs."""
    name = InputParam[str]("name")
    threshold = InputParam[float].with_default("threshold", 5.0)

    @dataclass(frozen=True)
    class TwoParamStep(Step[GreetResult]):
        def run(self, name: str, threshold: float) -> GreetResult:
            return GreetResult(greeting=f"{name}:{threshold}")

    result = TwoParamStep()(name=name, threshold=threshold)
    pipeline = Pipeline(output=result, cache_dir=tmp_path)

    assert pipeline.input_names == {"name", "threshold"}


def test_parameter_shared_across_steps(tmp_path: Path):
    """Same parameter can be used by multiple steps."""
    text = InputParam[str]("text")

    @dataclass(frozen=True)
    class UpperStep(Step[GreetResult]):
        """Uppercases text."""

        def run(self, text: str) -> GreetResult:
            return GreetResult(greeting=text.upper())

    @dataclass(frozen=True)
    class CombineStep(Step[GreetResult]):
        """Combines upper and length."""

        def run(self, upper: GreetResult, length: CountResult) -> GreetResult:
            return GreetResult(greeting=f"{upper.greeting} ({length.count})")

    upper_step = UpperStep()
    length_step = LengthStep()
    combine_step = CombineStep()

    upper_result = upper_step(text=text)
    length_result = length_step(text=text)
    combined = combine_step(upper=upper_result, length=length_result)

    pipeline = Pipeline(output=combined, cache_dir=tmp_path)
    output = pipeline.execute(text="hello")
    assert output.greeting == "HELLO (5)"
