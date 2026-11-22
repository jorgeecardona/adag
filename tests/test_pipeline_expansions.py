"""``Pipeline.expansions()`` lists the steps that build a sub-graph, and ``validate()`` checks them."""

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from adag import (
    FanOutStep,
    LoopStep,
    Pipeline,
    Step,
    StepResult,
    Template,
    UnboundInputError,
)


class Num(BaseModel):
    value: int


@dataclass(frozen=True)
class Seed(Step[Num]):
    def run(self) -> Num:
        return Num(value=1)


@dataclass(frozen=True)
class Double(Step[Num]):
    state: Num

    def run(self, state: Num) -> Num:
        return Num(value=state.value * 2)


@dataclass(frozen=True)
class NeedsExtra(Step[Num]):
    """``run`` asks for ``extra``, which nothing gives it."""

    state: Num

    def run(self, state: Num, extra: int) -> Num:
        return Num(value=state.value + extra)


def _big(state: Num) -> bool:
    return state.value > 100


def _loop(body: Any) -> StepResult[Any]:
    return LoopStep(body=Template(inputs=("state",), build=body), until=_big)(init=Seed()())


def _good(state: Any) -> StepResult[Any]:
    return Double(state=state)()


def _bad(state: Any) -> StepResult[Any]:
    return NeedsExtra(state=state)()


def test_a_pipeline_without_a_template_has_no_expansions() -> None:
    assert Pipeline(Seed()()).expansions() == ()


def test_a_loop_is_listed_with_the_steps_of_its_body() -> None:
    (found,) = Pipeline(_loop(_good)).expansions()
    assert found.expansion.shape == "loop"
    assert found.body is not None
    assert [type(r.step) for r in found.body] == [Double]


def test_a_template_that_reads_its_input_has_no_body() -> None:
    def reads(key: str, item: str) -> StepResult[Any]:
        return Double(state=Num(value=len(item)))()  # reads the item: a placeholder refuses

    fan = FanOutStep[str, Num](over={"a": 1}, template=Template(build=reads))()
    (found,) = Pipeline(fan).expansions()
    assert found.body is None


def test_validate_finds_an_unbound_input_inside_a_template() -> None:
    pipeline = Pipeline(_loop(_bad))
    with pytest.raises(UnboundInputError, match="In the template of"):
        pipeline.validate()


def test_validate_passes_for_a_good_template() -> None:
    Pipeline(_loop(_good)).validate()


def test_validate_looks_inside_a_loop_inside_a_fan_out() -> None:
    def branch(key: str, item: int) -> StepResult[Any]:
        return _loop(_bad)

    fan = FanOutStep[str, Num](over={"a": 1}, template=Template(build=branch))()
    with pytest.raises(UnboundInputError):
        Pipeline(fan).validate()
