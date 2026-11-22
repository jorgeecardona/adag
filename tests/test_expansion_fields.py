"""``Expansion`` says how a template node binds its inputs, so a frame can be drawn from it."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from adag import FanOutStep, LoopStep, Step, StepResult, Template


class Num(BaseModel):
    value: int


@dataclass(frozen=True)
class Seed(Step[Num]):
    value: int = 8

    def run(self, value: int) -> Num:
        return Num(value=value)


@dataclass(frozen=True)
class Halve(Step[Num]):
    state: Num

    def run(self, state: Num) -> Num:
        return Num(value=state.value // 2)


@dataclass(frozen=True)
class Measure(Step[Num]):
    text: str

    def run(self, text: str) -> Num:
        return Num(value=len(text))


def small(state: Num) -> bool:
    return state.value <= 1


def _halve(state: Any) -> StepResult[Any]:
    return Halve(state=state)()


def _loop(**options: Any) -> LoopStep:
    return LoopStep(body=Template(inputs=("state",), build=_halve), until=small, **options)


def _measure(key: str, item: str) -> Any:
    return Measure(text=item)()


def test_a_wired_fan_out_binds_over_to_the_item_and_has_no_count() -> None:
    step = FanOutStep[Any, Num](template=Template(build=_measure))
    expansion = step.expansion()
    assert expansion is not None
    assert expansion.binds == (("over", "item"),)
    assert expansion.count is None
    assert expansion.caption == "one branch per item"
    assert expansion.feedback == ""


def test_a_fan_out_over_a_static_dict_knows_its_branch_count() -> None:
    step = FanOutStep[Any, Num](
        over={"a": "x", "b": "yy", "c": "z"}, template=Template(build=_measure)
    )
    expansion = step.expansion()
    assert expansion is not None
    assert expansion.count == 3
    assert expansion.caption == "3 items"


def test_one_branch_reads_item_in_the_singular() -> None:
    step = FanOutStep[Any, Num](over={"a": "x"}, template=Template(build=_measure))
    expansion = step.expansion()
    assert expansion is not None
    assert expansion.caption == "1 item"


def test_a_loop_binds_init_to_state_and_feeds_the_result_back() -> None:
    expansion = _loop(max_rounds=7).expansion()
    assert expansion is not None
    assert expansion.binds == (("init", "state"),)
    assert expansion.feedback == "state"
    assert expansion.caption == "until small, at most 7 rounds"
    assert expansion.count is None


def test_a_loop_that_returns_at_the_limit_says_so() -> None:
    expansion = _loop(max_rounds=3, on_max="return").expansion()
    assert expansion is not None
    assert expansion.caption == "until small, at most 3 rounds, then returns the last state"


def test_a_factory_fan_out_has_no_expansion() -> None:
    step = FanOutStep[Any, Num](over={"a": "x"}, factory=_measure)
    assert step.expansion() is None
