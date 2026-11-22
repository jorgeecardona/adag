"""``Template`` and a ``FanOutStep`` whose ``over`` comes from an upstream node."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from adag import FanOutStep, InputParam, NullObserver, Pipeline, Step, StepResult, Template

# These tests cover max_workers and parallel, which are deprecated and removed in 1.0.
pytestmark = pytest.mark.filterwarnings("ignore::adag.AdagDeprecationWarning")

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


def test_a_dispatched_template_fan_out_needs_the_module_and_variable_of_the_worker() -> None:
    from adag import ResolveContext, SubprocessDispatcher

    node: Any = FanOutStep[Any, Size](over={"a": "xy"}, template=Template(build=_measure))()
    with pytest.raises(ValueError, match="pipeline_module and fan_out_var"):
        node.resolve(ResolveContext(dispatcher=SubprocessDispatcher()))


def test_the_static_form_still_works(tmp_path: Path) -> None:
    node: Any = FanOutStep[Any, Size](
        over={"a": "xy"}, factory=lambda key, text: Measure(text=text)()
    )()
    assert _run(node, tmp_path)["a"].chars == 2


# ── how a mapped node is shown ───────────────────────────────────────────────


def test_dag_draws_one_mapped_node_with_the_template_steps_inside_in_both_themes() -> None:
    from adag import dag
    from adag.visualization import SvgOptions

    target = _fan_out(InputParam[str]("text", value_type=str))
    for theme in ("light", "dark"):
        svg = str(dag(target, options=SvgOptions(theme=theme, frames=False)))
        stylesheet, drawing = svg.split("</style>", 1)
        assert ".dag-node--mapped" in stylesheet
        assert drawing.count("dag-node--mapped") == 1  # one node, not one per item
        assert "each item" in drawing and "Measure" in drawing  # the template, inside the node
        assert "known at run time" in drawing


def test_dag_draws_the_template_once_inside_a_frame_by_default() -> None:
    from adag import dag

    svg = str(dag(_fan_out(InputParam[str]("text", value_type=str))))
    drawing = svg.split("</style>", 1)[1]
    assert drawing.count("dag-frame--mapped") == 1  # one frame, not one per item
    assert "Measure" in drawing  # the body of the template, drawn inside
    assert "dag-node--mapped" not in drawing  # no single dashed box
    assert "one branch per item" in drawing


def test_dag_does_not_flag_a_factory_fan_out_as_mapped() -> None:
    from adag import dag

    static = FanOutStep[str, Size](over={"a": "x"}, factory=lambda key, text: Measure(text=text)())
    assert "dag-node--mapped" not in str(dag(static())).split("</style>", 1)[1]


def test_explain_key_says_the_branches_are_known_at_run_time() -> None:
    from adag import ComponentKind

    target = _fan_out(InputParam[str]("text", value_type=str))
    explanation = target.explain_key()
    expansion = [c for c in explanation.components if c.kind is ComponentKind.EXPANSION]
    assert len(expansion) == 1
    assert expansion[0].name == "mapped"
    assert "known at run time" in expansion[0].value
    assert expansion[0].digest == ""  # adds nothing to the key
    assert explanation.key == target.content_hash()
    assert "expansion" in str(explanation)


def test_explain_key_of_a_static_fan_out_has_no_expansion_component() -> None:
    from adag import ComponentKind

    static = FanOutStep[str, Size](over={"a": "x"}, factory=lambda key, text: Measure(text=text)())
    assert all(c.kind is not ComponentKind.EXPANSION for c in static().explain_key().components)


def test_redacted_keeps_the_expansion_component() -> None:
    from adag import ComponentKind

    target = _fan_out(InputParam[str]("text", value_type=str))
    redacted = target.explain_key().redacted()
    assert any(c.kind is ComponentKind.EXPANSION and c.value for c in redacted.components)


def test_run_tree_labels_each_template_branch_by_its_key(tmp_path: Path) -> None:
    from adag import RunTreeObserver

    observer = RunTreeObserver()
    target = _fan_out(InputParam[str]("text", value_type=str))
    Pipeline(target, cache_dir=tmp_path).execute(observer=observer, text="ab;cde")
    tree = observer.render_tree()
    assert "Measure [ab]" in tree and "Measure [cde]" in tree
