"""``Template`` and a ``FanOutStep`` whose ``over`` comes from an upstream node."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from adag import FanOutStep, InputParam, NullObserver, Pipeline, Step, StepResult, Template

CALLS: list[str] = []


class Size(BaseModel):
    chars: int


@dataclass(frozen=True)
class Measure(Step[Size]):
    text: str

    def run(self, text: str) -> Size:
        CALLS.append(text)
        return Size(chars=len(text))


@dataclass(frozen=True)
class Split(Step[dict[str, str]]):
    """Not cached: its result is a dict, so every run calls it. ``version`` changes its key."""

    version: int = 1

    def run(self, text: str, version: int) -> dict[str, str]:
        return {part: part for part in text.split(";") if part}


@dataclass(frozen=True)
class Total(Step[Size]):
    def run(self, sizes: dict[str, Size]) -> Size:
        return Size(chars=sum(size.chars for size in sizes.values()))


def _measure(key: str, item: str) -> Any:
    return Measure(text=item)()


def _fan_out(text: Any, version: int = 1, max_workers: int | None = None) -> StepResult[Any]:
    split = Split(version=version)(text=text)
    return FanOutStep[Any, Size](template=Template(build=_measure), max_workers=max_workers)(
        over=split
    )


@pytest.fixture(autouse=True)
def _clear_calls() -> None:
    CALLS.clear()


def _run(target: Any, cache: Path, **params: Any) -> Any:
    return Pipeline(target, cache_dir=cache).execute(observer=NullObserver(), **params)


def test_one_branch_per_item_the_upstream_returns(tmp_path: Path) -> None:
    result = _run(_fan_out(InputParam[str]("text", value_type=str)), tmp_path, text="ab;cde;f")
    assert {key: size.chars for key, size in result.items()} == {"ab": 2, "cde": 3, "f": 1}


def test_over_given_as_the_field_is_wired_the_same(tmp_path: Path) -> None:
    split = Split()(text=InputParam[str]("text", value_type=str))
    node: Any = FanOutStep[Any, Size](over=split, template=Template(build=_measure))()
    assert set(_run(node, tmp_path, text="ab;cde")) == {"ab", "cde"}


def test_threaded_branches_give_the_same_result(tmp_path: Path) -> None:
    target = _fan_out(InputParam[str]("text", value_type=str), max_workers=3)
    result = _run(target, tmp_path, text="ab;cde;f")
    assert {key: size.chars for key, size in result.items()} == {"ab": 2, "cde": 3, "f": 1}


def test_a_rerun_finds_every_branch_in_the_cache(tmp_path: Path) -> None:
    target = _fan_out(InputParam[str]("text", value_type=str))
    _run(target, tmp_path, text="ab;cde")
    assert sorted(CALLS) == ["ab", "cde"]
    CALLS.clear()
    _run(target, tmp_path, text="ab;cde")
    assert CALLS == []


def test_upstream_that_returns_the_same_items_hits_every_branch(tmp_path: Path) -> None:
    """Early cutoff: Split reruns under a new key, returns the same items, no branch runs."""
    _run(_fan_out(InputParam[str]("text", value_type=str), version=1), tmp_path, text="ab;cde")
    CALLS.clear()
    _run(_fan_out(InputParam[str]("text", value_type=str), version=2), tmp_path, text="ab;cde")
    assert CALLS == []


def test_a_changed_item_recomputes_only_its_branch(tmp_path: Path) -> None:
    target = _fan_out(InputParam[str]("text", value_type=str))
    _run(target, tmp_path, text="ab;cde;f")
    CALLS.clear()
    _run(target, tmp_path, text="ab;cdX;f")
    assert CALLS == ["cdX"]


def test_downstream_step_reads_the_branch_results(tmp_path: Path) -> None:
    sizes = _fan_out(InputParam[str]("text", value_type=str))
    total = Total()(sizes=sizes)
    assert _run(total, tmp_path, text="ab;cde").chars == 5


def test_node_key_follows_the_upstream_key_and_the_template() -> None:
    text: Any = InputParam[str]("text", value_type=str)
    base = _fan_out(text).content_hash()
    assert base == _fan_out(text).content_hash()
    assert base != _fan_out(text, version=2).content_hash()

    def other_build(key: str, item: str) -> Any:
        return Measure(text="fixed")()

    other: Any = FanOutStep[Any, Size](template=Template(build=other_build))(
        over=Split()(text=text)
    )
    assert other.content_hash() != base


def test_keys_that_differ_only_as_text_raise(tmp_path: Path) -> None:
    @dataclass(frozen=True)
    class Pairs(Step[dict[Any, str]]):
        def run(self) -> dict[Any, str]:
            return {1: "a", "1": "b"}

    node: Any = FanOutStep[Any, Size](template=Template(build=_measure))(over=Pairs()())
    with pytest.raises(ValueError, match="same text"):
        _run(node, tmp_path)


def test_an_item_the_cache_key_cannot_encode_raises_before_any_branch_runs(tmp_path: Path) -> None:
    @dataclass(frozen=True)
    class Objects(Step[dict[str, Any]]):
        def run(self) -> dict[str, Any]:
            return {"a": object()}

    node: Any = FanOutStep[Any, Size](template=Template(build=_measure))(over=Objects()())
    with pytest.raises(TypeError, match="item 'a'"):
        _run(node, tmp_path)
    assert CALLS == []


def test_over_must_resolve_to_a_dict(tmp_path: Path) -> None:
    @dataclass(frozen=True)
    class Listing(Step[list[str]]):
        def run(self) -> list[str]:
            return ["a"]

    node: Any = FanOutStep[Any, Size](template=Template(build=_measure))(over=Listing()())
    with pytest.raises(TypeError, match="must be a dict"):
        _run(node, tmp_path)


def test_a_wired_over_needs_a_template() -> None:
    with pytest.raises(TypeError, match="needs template="):
        FanOutStep[Any, Size](over=Split()(text="a;b"), factory=_measure)

    with pytest.raises(TypeError, match="needs template="):
        FanOutStep[Any, Size](factory=_measure)(over=Split()(text="a;b"))


def test_a_fan_out_needs_one_of_factory_or_template() -> None:
    with pytest.raises(TypeError, match="exactly one"):
        FanOutStep[Any, Size](over={"a": "b"})
    with pytest.raises(TypeError, match="exactly one"):
        FanOutStep[Any, Size](over={"a": "b"}, factory=_measure, template=Template(build=_measure))


def test_a_template_with_a_static_over_runs_too(tmp_path: Path) -> None:
    node: Any = FanOutStep[Any, Size](
        over={"a": "xy", "b": "z"}, template=Template(build=_measure)
    )()
    assert {key: size.chars for key, size in _run(node, tmp_path).items()} == {"a": 2, "b": 1}


def test_a_template_fan_out_is_not_dispatched() -> None:
    from adag import ResolveContext, SubprocessDispatcher

    node: Any = FanOutStep[Any, Size](over={"a": "xy"}, template=Template(build=_measure))()
    with pytest.raises(NotImplementedError, match="in this process only"):
        node.resolve(ResolveContext(dispatcher=SubprocessDispatcher()))


def test_the_static_form_still_works(tmp_path: Path) -> None:
    node: Any = FanOutStep[Any, Size](
        over={"a": "xy"}, factory=lambda key, text: Measure(text=text)()
    )()
    assert _run(node, tmp_path)["a"].chars == 2
