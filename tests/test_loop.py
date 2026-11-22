"""``LoopStep``: rounds that feed each other, cached by the state each one starts from."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from adag import (
    ComponentKind,
    FanOutStep,
    LoopNotConverged,
    LoopStep,
    NullObserver,
    Pipeline,
    RunTreeObserver,
    Step,
    StepResult,
    Template,
)

CALLS: list[int] = []
STATE = {"explode_at": -1}


class Num(BaseModel):
    value: int


class Many(BaseModel):
    values: dict[str, int]


@dataclass(frozen=True)
class Seed(Step[Num]):
    """``version`` changes the key of this node, not the number it returns."""

    value: int
    version: int = 1

    def run(self, value: int, version: int) -> Num:
        return Num(value=value)


@dataclass(frozen=True)
class Halve(Step[Num]):
    state: Num

    def run(self, state: Num) -> Num:
        CALLS.append(state.value)
        if state.value == STATE["explode_at"]:
            raise RuntimeError("the machine went down")
        return Num(value=state.value // 2)


@dataclass(frozen=True)
class Probe(Step[Num]):
    """Succeeds on its second call, like a service that is down at first."""

    state: Num
    turn: int

    def run(self, state: Num, turn: int) -> Num:
        CALLS.append(turn)
        return Num(value=1 if len(CALLS) >= 2 else 0)


@dataclass(frozen=True)
class Items(Step[dict[str, int]]):
    state: Many

    def run(self, state: Many) -> dict[str, int]:
        return state.values


@dataclass(frozen=True)
class HalveOne(Step[Num]):
    value: int

    def run(self, value: int) -> Num:
        CALLS.append(value)
        return Num(value=value // 2)


@dataclass(frozen=True)
class Collect(Step[Many]):
    def run(self, parts: dict[str, Num]) -> Many:
        return Many(values={key: num.value for key, num in parts.items()})


def small(state: Num) -> bool:
    return state.value <= 5


def zero(state: Num) -> bool:
    return state.value == 0


def never(state: Num) -> bool:
    return False


def all_small(state: Many) -> bool:
    return all(value <= 5 for value in state.values.values())


def good(state: Num) -> bool:
    return state.value == 1


def _halve(state: Any) -> StepResult[Any]:
    return Halve(state=state)()


def _two_inputs(state: Any, turn: Any) -> StepResult[Any]:
    return _halve(state)


def _loop(start: int, until: Any = small, max_rounds: int = 20, **options: Any) -> Any:
    body = Template(inputs=("state",), build=_halve)
    step = LoopStep(body=body, until=until, max_rounds=max_rounds, **options)
    return step(init=Seed(value=start)())


@pytest.fixture(autouse=True)
def _reset() -> None:
    CALLS.clear()
    STATE["explode_at"] = -1


def _run(target: Any, cache: Path) -> Any:
    return Pipeline(target, cache_dir=cache).execute(observer=NullObserver())


def test_stops_when_until_is_true(tmp_path: Path) -> None:
    assert _run(_loop(40), tmp_path) == Num(value=5)
    assert CALLS == [40, 20, 10]


def test_a_start_state_that_already_satisfies_until_runs_no_round(tmp_path: Path) -> None:
    assert _run(_loop(3), tmp_path) == Num(value=3)
    assert CALLS == []


def test_stops_at_a_fixed_point(tmp_path: Path) -> None:
    assert _run(_loop(3, until=never), tmp_path) == Num(value=0)
    assert CALLS == [3, 1, 0]  # 0 halves to 0: the state did not change


def test_max_rounds_raises_and_carries_the_last_state(tmp_path: Path) -> None:
    with pytest.raises(LoopNotConverged) as caught:
        _run(_loop(1000, until=zero, max_rounds=3), tmp_path)
    assert caught.value.rounds == 3
    assert caught.value.state == Num(value=125)


def test_on_max_return_gives_the_last_state(tmp_path: Path) -> None:
    result = _run(_loop(1000, until=zero, max_rounds=3, on_max="return"), tmp_path)
    assert result == Num(value=125)


def test_a_higher_max_rounds_reuses_the_rounds_that_ran(tmp_path: Path) -> None:
    with pytest.raises(LoopNotConverged):
        _run(_loop(1000, until=zero, max_rounds=3), tmp_path)
    assert CALLS == [1000, 500, 250]
    CALLS.clear()
    _run(_loop(1000, until=zero, max_rounds=5, on_max="return"), tmp_path)
    assert CALLS == [125, 62]  # rounds 0 to 2 are in the cache


def test_a_crashed_loop_continues_after_its_last_cached_round(tmp_path: Path) -> None:
    STATE["explode_at"] = 125
    with pytest.raises(RuntimeError, match="went down"):
        _run(_loop(1000, until=zero), tmp_path)
    assert CALLS == [1000, 500, 250, 125]
    CALLS.clear()
    STATE["explode_at"] = -1
    assert _run(_loop(1000, until=zero), tmp_path) == Num(value=0)
    assert CALLS[0] == 125  # the three finished rounds were not run again


def test_a_new_upstream_key_with_the_same_state_hits_every_round(tmp_path: Path) -> None:
    _run(_loop(40), tmp_path)
    CALLS.clear()
    body = Template(inputs=("state",), build=_halve)
    again = LoopStep(body=body, until=small)(init=Seed(value=40, version=2)())
    assert _run(again, tmp_path) == Num(value=5)
    assert CALLS == []


def test_a_rerun_with_a_cache_calls_no_step(tmp_path: Path) -> None:
    _run(_loop(40), tmp_path)
    CALLS.clear()
    assert _run(_loop(40), tmp_path) == Num(value=5)
    assert CALLS == []


def test_a_lambda_or_a_nested_function_is_refused() -> None:
    body = Template(inputs=("state",), build=_halve)
    with pytest.raises(TypeError, match="module level"):
        LoopStep(body=body, until=lambda state: True)

    def nested(state: Num) -> bool:
        return True

    with pytest.raises(TypeError, match="module level"):
        LoopStep(body=body, until=nested)


def test_until_version_and_max_rounds_change_the_loop_key_but_not_a_round_key() -> None:
    base = _loop(40)
    assert _loop(40, until_version="2").content_hash() != base.content_hash()
    assert _loop(40, max_rounds=7).content_hash() != base.content_hash()
    assert _loop(40, until=zero).content_hash() != base.content_hash()
    template = Template(inputs=("state",), build=_halve)
    assert (
        template.bind(Num(value=40)).content_hash() == template.bind(Num(value=40)).content_hash()
    )


def test_the_same_state_gives_the_same_key_so_a_retry_never_retries(tmp_path: Path) -> None:
    def by_round(state: Any, turn: Any) -> StepResult[Any]:
        return Probe(state=state, turn=turn)()

    def same_round(state: Any) -> StepResult[Any]:
        return Probe(state=state, turn=0)()

    stuck = LoopStep(body=Template(inputs=("state",), build=same_round), until=good)
    assert _run(stuck(init=Seed(value=0)()), tmp_path) == Num(value=0)  # fixed point
    assert CALLS == [0]
    CALLS.clear()

    body = Template(inputs=("state", "round"), build=by_round)
    retry = LoopStep(body=body, until=good, key_by_round=True)
    assert _run(retry(init=Seed(value=0)()), tmp_path / "retry") == Num(value=1)
    assert CALLS == [0, 1]  # the round number made round 1 a new key


def test_key_by_round_needs_a_body_with_a_round_input() -> None:
    body = Template(inputs=("state",), build=_halve)
    with pytest.raises(TypeError, match="inputs"):
        LoopStep(body=body, until=small, key_by_round=True)
    both = Template(inputs=("state", "round"), build=_two_inputs)
    with pytest.raises(TypeError, match="inputs"):
        LoopStep(body=both, until=small)


def test_a_loop_without_a_start_state_is_refused(tmp_path: Path) -> None:
    body = Template(inputs=("state",), build=_halve)
    with pytest.raises(TypeError, match="start state"):
        _run(LoopStep(body=body, until=small)(), tmp_path)


def test_a_loop_inside_a_fan_out_branch(tmp_path: Path) -> None:
    def branch(key: str, item: int) -> StepResult[Any]:
        body = Template(inputs=("state",), build=_halve)
        return LoopStep(body=body, until=small)(init=Seed(value=item)())

    fan = FanOutStep[str, Num](over={"a": 40, "b": 12}, template=Template(build=branch))()
    result = _run(fan, tmp_path)
    assert result == {"a": Num(value=5), "b": Num(value=3)}


def _fan_body(state: Any) -> StepResult[Any]:
    def one(key: str, item: int) -> StepResult[Any]:
        return HalveOne(value=item)()

    fan = FanOutStep[str, Num](template=Template(build=one))(over=Items(state=state)())
    return Collect()(parts=fan)


def test_a_fan_out_inside_a_loop_body(tmp_path: Path) -> None:
    body = Template(inputs=("state",), build=_fan_body)
    loop = LoopStep(body=body, until=all_small)(init=_many({"a": 40, "b": 12}))
    assert _run(loop, tmp_path) == Many(values={"a": 5, "b": 1})
    assert sorted(CALLS) == [3, 6, 10, 12, 20, 40]  # every branch of every round ran once


@dataclass(frozen=True)
class MakeMany(Step[Many]):
    values: dict[str, int]

    def run(self, values: dict[str, int]) -> Many:
        return Many(values=values)


def _many(values: dict[str, int]) -> StepResult[Any]:
    return MakeMany(values=values)()


def test_the_graph_draws_one_loop_node_and_explain_key_lists_the_expansion() -> None:
    from adag import dag
    from adag.visualization import SvgOptions

    target = _loop(40)
    for theme in ("light", "dark"):
        svg = str(dag(target, options=SvgOptions(theme=theme, frames=False)))
        stylesheet, drawing = svg.split("</style>", 1)
        assert ".dag-node--loop" in stylesheet
        assert drawing.count("dag-node--loop") == 1
        assert "each round" in drawing and "Halve" in drawing
    explanation = target.explain_key()
    expansion = [c for c in explanation.components if c.kind is ComponentKind.EXPANSION]
    assert [c.name for c in expansion] == ["loop"]
    assert "up to 20 rounds" in expansion[0].value
    assert explanation.key == target.content_hash()


def test_the_loop_is_drawn_once_inside_a_frame_with_a_way_back_by_default() -> None:
    from adag import dag

    drawing = str(dag(_loop(40))).split("</style>", 1)[1]
    assert drawing.count("dag-frame--loop") == 1
    assert "dag-edge--feedback" in drawing  # the one place where adag draws a cycle
    assert "until small" in drawing or "until " in drawing
    assert "Halve" in drawing


def test_the_observer_hears_each_round_and_the_run_tree_labels_it(tmp_path: Path) -> None:
    class Rounds(NullObserver):
        def __init__(self) -> None:
            self.heard: list[tuple[int, str]] = []

        def on_loop_round(self, number: int, key: str) -> None:
            self.heard.append((number, key))

    rounds = Rounds()
    Pipeline(_loop(40), cache_dir=tmp_path).execute(observer=rounds)
    assert [number for number, _ in rounds.heard] == [0, 1, 2]
    assert all(len(key) == 12 for _, key in rounds.heard)

    tree = RunTreeObserver()
    Pipeline(_loop(40), cache_dir=tmp_path / "tree").execute(observer=tree)
    assert "Halve [round 0]" in tree.render_tree() and "Halve [round 2]" in tree.render_tree()


def test_a_state_that_cannot_be_encoded_is_refused(tmp_path: Path) -> None:
    body = Template(inputs=("state",), build=_halve)
    from adag.hashing import UnhashableValueError

    @dataclass(frozen=True)
    class Odd(Step[Any]):
        def run(self) -> Any:
            return object()

    with pytest.raises(UnhashableValueError):
        _run(LoopStep(body=body, until=small)(init=Odd()()), tmp_path)
