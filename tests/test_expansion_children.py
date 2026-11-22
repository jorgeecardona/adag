"""A fan-out or a loop writes the keys of its branches or rounds into its key record."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from adag import FanOutStep, LoopStep, NullObserver, Pipeline, Step, StepResult, Template


class Num(BaseModel):
    value: int


@dataclass(frozen=True)
class Seed(Step[Num]):
    def run(self) -> Num:
        return Num(value=40)


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
    return state.value < 10


def _halve(state: Any) -> StepResult[Any]:
    return Halve(state=state)()


def _measure(key: str, item: str) -> StepResult[Any]:
    return Measure(text=item)()


def _record(cache: Path, node: StepResult[Any]) -> dict[str, Any]:
    key = node.content_hash()
    return json.loads((cache / ".keys" / f"{key}.json").read_text())


def test_a_loop_lists_the_key_of_each_round(tmp_path: Path) -> None:
    node = LoopStep(body=Template(inputs=("state",), build=_halve), until=small)(init=Seed()())
    Pipeline(node, cache_dir=tmp_path).execute(observer=NullObserver())  # 40 -> 20 -> 10 -> 5
    children = _record(tmp_path, node)["expansion"]["children"]
    assert len(children) == 3
    for key in children:
        assert (tmp_path / ".keys" / f"{key}.json").exists()  # each round has its own record


def test_a_fan_out_lists_the_key_of_each_branch(tmp_path: Path) -> None:
    over = {"a": "x", "b": "yy", "c": "zzz"}
    node = FanOutStep[str, Num](over=over, template=Template(build=_measure))()
    Pipeline(node, cache_dir=tmp_path).execute(observer=NullObserver())
    children = _record(tmp_path, node)["expansion"]["children"]
    assert children == [_measure(k, v).content_hash() for k, v in over.items()]


def test_a_step_with_no_template_has_no_expansion_record(tmp_path: Path) -> None:
    node = Seed()()
    Pipeline(node, cache_dir=tmp_path).execute(observer=NullObserver())
    assert "expansion" not in _record(tmp_path, node)


def test_the_template_hash_does_not_change_the_key() -> None:
    node = LoopStep(body=Template(inputs=("state",), build=_halve), until=small)(init=Seed()())
    assert node.explain_key().key == node.content_hash()
