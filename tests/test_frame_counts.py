"""With ``cache_dir`` a frame's caption says what the cache holds for its branches or rounds."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from adag import (
    FanOutStep,
    InputParam,
    LoopStep,
    NullObserver,
    Pipeline,
    Step,
    StepResult,
    Template,
    dag,
)


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


@dataclass(frozen=True)
class Split(Step[dict[str, str]]):
    def run(self, text: str) -> dict[str, str]:
        return {part: part for part in text.split(";")}


def small(state: Num) -> bool:
    return state.value < 10


def _halve(state: Any) -> StepResult[Any]:
    return Halve(state=state)()


def _measure(key: str, item: str) -> StepResult[Any]:
    return Measure(text=item)()


def _loop() -> StepResult[Any]:
    return LoopStep(body=Template(inputs=("state",), build=_halve), until=small)(init=Seed()())


def _static_fan_out() -> StepResult[Any]:
    over = {"a": "x", "b": "yy", "c": "zzz"}
    return FanOutStep[str, Num](over=over, template=Template(build=_measure))()


def _wired_fan_out() -> StepResult[Any]:
    text = InputParam.with_default("text", "x;yy;zzz")
    return FanOutStep[str, Num](template=Template(build=_measure))(over=Split()(text=text))


def _caption(svg: Any) -> str:
    text = str(svg)
    return text.split("</style>", 1)[1]


def test_nothing_is_said_without_a_cache_dir() -> None:
    assert "cached" not in _caption(dag(_static_fan_out()))


def test_a_static_fan_out_counts_its_cached_branches(tmp_path: Path) -> None:
    node = _static_fan_out()
    assert "3 items, 0 cached" in _caption(dag(node, cache_dir=tmp_path))
    Pipeline(node, cache_dir=tmp_path).execute(observer=NullObserver())
    assert "3 items, 3 cached" in _caption(dag(node, cache_dir=tmp_path))


def test_a_loop_says_how_many_rounds_the_last_run_had(tmp_path: Path) -> None:
    node = _loop()
    assert "last run" not in _caption(dag(node, cache_dir=tmp_path))
    Pipeline(node, cache_dir=tmp_path).execute(observer=NullObserver())  # 40 -> 20 -> 10 -> 5
    caption = _caption(dag(node, cache_dir=tmp_path))
    assert "last run: 3 rounds, 3 cached" in caption
    assert "until small" in caption  # the stop test stays in the caption


def test_a_wired_fan_out_uses_the_record_of_the_last_run(tmp_path: Path) -> None:
    node = _wired_fan_out()
    assert "cached" not in _caption(dag(node, cache_dir=tmp_path))
    Pipeline(node, cache_dir=tmp_path).execute(observer=NullObserver())
    assert "last run: 3 items, 3 cached" in _caption(dag(node, cache_dir=tmp_path))
