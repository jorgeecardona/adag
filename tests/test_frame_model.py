"""``walk(expand=True)``: the body of a fan-out or a loop as children of its node."""

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from adag import FanOutStep, InputParam, LoopStep, Step, StepResult, Template, dag
from adag import visualization as viz
from adag.visualization import (  # pyright: ignore[reportPrivateUsage]
    Edge,
    NodeMetadata,
    SvgOptions,
    _collapse,
    node_metadata,
    walk,
)


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


@dataclass(frozen=True)
class Add(Step[Num]):
    delta: Any

    def run(self, delta: int, base: Num) -> Num:
        return Num(value=base.value + delta)


@dataclass(frozen=True)
class Split(Step[dict[str, str]]):
    def run(self, text: str) -> dict[str, str]:
        return {part: part for part in text.split(";")}


def small(state: Num) -> bool:
    return state.value <= 1


def _halve(state: Any) -> StepResult[Any]:
    return Halve(state=state)()


def _measure(key: str, item: str) -> Any:
    return Measure(text=item)()


def _loop(max_rounds: int = 7, start: Any = None) -> StepResult[Any]:
    body = Template(inputs=("state",), build=_halve)
    return LoopStep(body=body, until=small, max_rounds=max_rounds)(init=start or Seed(value=40)())


def _wired_fan_out() -> StepResult[Any]:
    split = Split()(text=InputParam("text"))
    return FanOutStep[Any, Num](template=Template(build=_measure))(over=split)


def _nodes(output: Any) -> dict[str, Any]:
    nodes, _ = walk(output, expand=True)
    return {n.name: n for n in nodes}


def _edges(output: Any) -> set[tuple[str, str, str]]:
    _, edges = walk(output, expand=True)
    return {(e.source, e.target, e.label) for e in edges}


def test_the_default_walk_is_flat() -> None:
    nodes, edges = walk(_wired_fan_out())
    assert {n.name for n in nodes} == {"text", "Split", "FanOutStep"}
    assert all(n.parent == "" for n in nodes)
    assert {(e.source, e.target) for e in edges} == {("text", "Split"), ("Split", "FanOutStep")}


def test_a_wired_fan_out_has_its_body_inside() -> None:
    nodes = _nodes(_wired_fan_out())
    assert nodes["FanOutStep/item"].kind == "template_input"
    assert nodes["FanOutStep/item"].parent == "FanOutStep"
    assert nodes["FanOutStep/Measure"].parent == "FanOutStep"
    assert nodes["Split"].parent == ""
    assert "FanOutStep/key" not in nodes  # the body does not use the key


def test_the_edge_into_the_fan_out_ends_at_its_item_input() -> None:
    edges = _edges(_wired_fan_out())
    assert ("Split", "FanOutStep/item", "over") in edges
    assert ("Split", "FanOutStep", "over") not in edges
    assert ("FanOutStep/item", "FanOutStep/Measure", "text") in edges


def test_a_loop_has_a_state_input_that_the_init_edge_enters() -> None:
    nodes = _nodes(_loop())
    edges = _edges(_loop())
    assert nodes["LoopStep/state"].kind == "template_input"
    assert nodes["LoopStep/Halve"].parent == "LoopStep"
    assert ("Seed", "LoopStep/state", "init") in edges
    assert ("LoopStep/state", "LoopStep/Halve", "state") in edges


def test_a_loop_with_a_static_start_shows_the_value_on_its_state_input() -> None:
    body = Template(inputs=("state",), build=_halve)
    step = LoopStep(body=body, until=small, init=Num(value=9))
    nodes = _nodes(step())
    assert "value=9" in nodes["LoopStep/state"].value


def test_a_loop_in_each_branch_of_a_fan_out_nests_two_frames() -> None:
    def branch(key: str, item: int) -> StepResult[Num]:
        return LoopStep(body=Template(inputs=("state",), build=_halve), until=small)(
            init=Seed(value=item)()
        )

    each = FanOutStep[str, Num](over={"a": 40, "b": 12}, template=Template(build=branch))()
    nodes = _nodes(each)
    assert nodes["FanOutStep/LoopStep"].parent == "FanOutStep"
    assert nodes["FanOutStep/LoopStep/Halve"].parent == "FanOutStep/LoopStep"
    assert nodes["FanOutStep/LoopStep/state"].kind == "template_input"
    edges = _edges(each)
    assert ("FanOutStep/Seed", "FanOutStep/LoopStep/state", "init") in edges


def test_a_frame_deeper_than_the_limit_stays_one_node(monkeypatch: pytest.MonkeyPatch) -> None:
    def branch(key: str, item: int) -> StepResult[Num]:
        return LoopStep(body=Template(inputs=("state",), build=_halve), until=small)(
            init=Seed(value=item)()
        )

    monkeypatch.setattr(viz, "_MAX_FRAME_DEPTH", 1)
    each = FanOutStep[str, Num](over={"a": 40}, template=Template(build=branch))()
    nodes = _nodes(each)
    assert "FanOutStep/LoopStep" in nodes
    assert not any(n.startswith("FanOutStep/LoopStep/") for n in nodes)


def test_a_step_the_template_reaches_through_a_closure_stays_outside() -> None:
    shared = Seed(value=3)()

    def build(key: str, item: Any) -> StepResult[Any]:
        return Add(delta=item)(base=shared)

    fan = FanOutStep[str, Num](over={"a": 1, "b": 2}, template=Template(build=build))()
    nodes = _nodes(fan)
    edges = _edges(fan)
    assert nodes["Seed"].parent == ""  # drawn outside the frame
    assert nodes["FanOutStep/Seed"].kind == "template_input"
    assert ("Seed", "FanOutStep/Seed", "captured") in edges
    assert ("FanOutStep/Seed", "FanOutStep/Add", "base") in edges
    assert ("FanOutStep/item", "FanOutStep/Add", "delta") in edges


def test_a_template_that_reads_its_input_stays_one_node() -> None:
    def build(key: str, item: str) -> Any:
        return Measure(text=item.upper())()

    fan = FanOutStep[str, Num](over={"a": "x"}, template=Template(build=build))()
    nodes = _nodes(fan)
    assert set(nodes) == {"FanOutStep"}


def test_a_factory_fan_out_has_no_frame() -> None:
    fan = FanOutStep[str, Num](over={"a": "x"}, factory=_measure)()
    assert set(_nodes(fan)) == {"FanOutStep"}


def test_the_metadata_of_a_frame_has_a_caption_and_its_children_have_no_state() -> None:
    nodes, _ = walk(_loop(), expand=True)
    by_name = {m.name: m for m in node_metadata(nodes)}
    frame = by_name["LoopStep"]
    assert frame.shape == "loop"
    assert frame.caption == "until small, at most 7 rounds"
    assert frame.feedback == "LoopStep/state"
    assert by_name["LoopStep/Halve"].state == "NONE"
    assert by_name["LoopStep/Halve"].key == ""
    assert by_name["LoopStep/Halve"].title == "Halve"
    assert by_name["LoopStep/state"].state == "SOURCE"
    assert by_name["LoopStep/state"].parent == "LoopStep"


def test_the_flat_walk_has_no_caption() -> None:
    nodes, _ = walk(_loop())
    by_name = {m.name: m for m in node_metadata(nodes)}
    assert by_name["LoopStep"].caption == ""
    assert by_name["LoopStep"].feedback == ""


def test_collapse_folds_the_children_into_their_frame() -> None:
    nodes = {
        "In": NodeMetadata("In", "param", "In"),
        "Each": NodeMetadata("Each", "step", "Each", shape="mapped"),
        "Each/item": NodeMetadata("Each/item", "template_input", "item", parent="Each"),
        "Each/A": NodeMetadata("Each/A", "step", "A", parent="Each"),
        "Each/B": NodeMetadata("Each/B", "step", "B", parent="Each"),
        "Out": NodeMetadata("Out", "step", "Out"),
    }
    edges = [
        ("In", "Each/item"),
        ("Each/item", "Each/A"),
        ("Each/A", "Each/B"),
        ("Each", "Out"),
    ]
    kept, kept_edges = _collapse(nodes, edges)
    assert list(kept) == ["In", "Each", "Out"]
    assert kept_edges == [("In", "Each"), ("Each", "Out")]


def test_collapse_to_one_level_keeps_the_first_frame_and_folds_the_second() -> None:
    nodes = {
        "F": NodeMetadata("F", "step", "F", shape="mapped"),
        "F/L": NodeMetadata("F/L", "step", "L", shape="loop", parent="F"),
        "F/L/H": NodeMetadata("F/L/H", "step", "H", parent="F/L"),
    }
    kept, _ = _collapse(nodes, [("F/L", "F/L/H")], depth=1)
    assert list(kept) == ["F", "F/L"]


def test_a_graph_without_frames_collapses_to_itself() -> None:
    nodes = {"A": NodeMetadata("A", "step", "A")}
    kept, kept_edges = _collapse(nodes, [("A", "A")])
    assert kept is nodes
    assert kept_edges == [("A", "A")]


FRAME_MARK = 'data-kind="frame"'  # the stylesheet also names `dag-frame`: look for an element


@pytest.mark.parametrize("make", [_loop, _wired_fan_out])
def test_without_frames_the_drawing_is_the_one_box_of_before(make: Any) -> None:
    target = make()
    flat_nodes, flat_edges = walk(target)
    flat = viz._render_svg(target, flat_nodes, flat_edges)  # pyright: ignore[reportPrivateUsage]
    assert str(dag(target, options=SvgOptions(frames=False))) == flat
    assert FRAME_MARK not in flat
    assert FRAME_MARK in str(dag(target))


@pytest.mark.parametrize("make", [_loop, _wired_fan_out])
def test_classic_never_draws_a_frame(make: Any) -> None:
    assert FRAME_MARK not in str(dag(make(), options=SvgOptions(style="classic")))


def test_edges_compare_by_value() -> None:
    assert Edge("a", "b", "x") == Edge("a", "b", "x")
