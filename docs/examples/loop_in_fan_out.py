"""A LoopStep inside each branch of a FanOutStep: halve every number until it is at most 5."""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import FanOutStep, LoopStep, NullObserver, Pipeline, Step, StepResult, Template


class Num(BaseModel):
    value: int


@dataclass(frozen=True)
class Seed(Step[Num]):
    value: int

    def run(self, value: int) -> Num:
        return Num(value=value)


@dataclass(frozen=True)
class Halve(Step[Num]):
    state: Num

    def run(self, state: Num) -> Num:
        return Num(value=state.value // 2)


def small(state: Num) -> bool:
    return state.value <= 5


def halve_once(state: Num) -> StepResult[Num]:
    return Halve(state=state)()


def branch(key: str, item: int) -> StepResult[Num]:
    body = Template(inputs=("state",), build=halve_once)
    return LoopStep(body=body, until=small)(init=Seed(value=item)())


each = FanOutStep[str, Num](over={"a": 40, "b": 12}, template=Template(build=branch))()

with tempfile.TemporaryDirectory() as cache:
    result = Pipeline(each, cache_dir=Path(cache)).execute(observer=NullObserver())
    print({key: num.value for key, num in result.items()})
