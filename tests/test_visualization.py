"""Tests for DAG visualization — walk, nodes, edges."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import InputParam, Step
from adag.io import FileSource
from adag.visualization import _edge_tail_parts, walk


class Result(BaseModel):
    """Simple result for testing."""

    value: int


@dataclass(frozen=True)
class LeafStep(Step[Result]):
    """Leaf step with no upstream."""

    def run(self, x: int) -> Result:
        return Result(value=x)


@dataclass(frozen=True)
class MiddleStep(Step[Result]):
    """Middle step with one upstream."""

    def run(self, source: Result) -> Result:
        return Result(value=source.value * 2)


@dataclass(frozen=True)
class FinalStep(Step[Result]):
    """Final step with upstream and file source."""

    def run(self, middle: Result, data: str) -> Result:
        return Result(value=middle.value + len(data))


def test_walk_single_step():
    """Walk a single step with no upstream."""
    x = InputParam[int]("x")
    step = LeafStep()
    result = step(x=x)
    nodes, edges = walk(result)
    node_names = [n.name for n in nodes]
    assert "LeafStep" in node_names
    assert "x" in node_names


def test_walk_chain():
    """Walk a two-step chain."""
    x = InputParam[int]("x")
    leaf = LeafStep()
    middle = MiddleStep()
    leaf_result = leaf(x=x)
    middle_result = middle(source=leaf_result)
    nodes, edges = walk(middle_result)
    node_names = [n.name for n in nodes]
    assert "LeafStep" in node_names
    assert "MiddleStep" in node_names
    edge_pairs = [(e.source, e.target) for e in edges]
    assert ("LeafStep", "MiddleStep") in edge_pairs


def test_walk_with_file_source(tmp_path: Path):
    """FileSource appears as a node."""
    file_path = tmp_path / "input.txt"
    file_path.write_text("test")
    x = InputParam[int]("x")
    leaf = LeafStep()
    middle = MiddleStep()
    final = FinalStep()
    leaf_result = leaf(x=x)
    middle_result = middle(source=leaf_result)
    final_result = final(middle=middle_result, data=FileSource(str(file_path)))
    nodes, edges = walk(final_result)
    node_names = [n.name for n in nodes]
    assert "input.txt" in node_names
    edge_pairs = [(e.source, e.target) for e in edges]
    assert ("input.txt", "FinalStep") in edge_pairs


def test_walk_shared_upstream():
    """Shared upstream step appears as a single node."""
    x = InputParam[int]("x")

    @dataclass(frozen=True)
    class BranchA(Step[Result]):
        """Branch A."""

        def run(self, source: Result) -> Result:
            return Result(value=source.value + 1)

    @dataclass(frozen=True)
    class BranchB(Step[Result]):
        """Branch B."""

        def run(self, source: Result) -> Result:
            return Result(value=source.value + 2)

    @dataclass(frozen=True)
    class Merge(Step[Result]):
        """Merge step."""

        def run(self, a: Result, b: Result) -> Result:
            return Result(value=a.value + b.value)

    leaf = LeafStep()
    leaf_result = leaf(x=x)
    a_result = BranchA()(source=leaf_result)
    b_result = BranchB()(source=leaf_result)
    merge_result = Merge()(a=a_result, b=b_result)

    nodes, edges = walk(merge_result)
    node_names = [n.name for n in nodes]
    assert node_names.count("LeafStep") == 1
    edge_pairs = [(e.source, e.target) for e in edges]
    assert ("LeafStep", "BranchA") in edge_pairs
    assert ("LeafStep", "BranchB") in edge_pairs
    assert ("BranchA", "Merge") in edge_pairs
    assert ("BranchB", "Merge") in edge_pairs


def test_walk_two_instances_same_class_are_distinct_nodes():
    """Two DISTINCT instances of the same Step class are two nodes (not collapsed) — a parent→child chain
    of the same kind. `node_label` gives each its own key+title; without it, the key is disambiguated.
    """
    x = InputParam[int]("x")
    parent = MiddleStep()
    child = MiddleStep()  # same class, different instance — the multi-cluster / drill-chain case
    chain = child(source=parent(source=LeafStep()(x=x)))
    nodes, edges = walk(chain)
    middles = [n for n in nodes if n.kind == "step" and isinstance(n.step, MiddleStep)]
    assert len(middles) == 2  # NOT collapsed into one self-looping node
    edge_pairs = [(e.source, e.target) for e in edges]
    names = {n.name for n in middles}
    assert len(names) == 2
    assert any(
        s in names and t in names for s, t in edge_pairs
    )  # an edge connects the two distinct instances


def test_walk_repeated_class_keeps_upstream_subtree():
    """Regression: class-keyed nodes skipped the 2nd same-class instance — dropping its upstream
    subtree and self-looping the edge. Instance keying keeps the full leaf -> parent -> child chain.
    """
    x = InputParam[int]("x")
    chain = MiddleStep()(source=MiddleStep()(source=LeafStep()(x=x)))
    nodes, edges = walk(chain)
    assert sum(1 for n in nodes if isinstance(n.step, MiddleStep)) == 2
    assert any(isinstance(n.step, LeafStep) for n in nodes)  # deepest upstream NOT dropped
    assert all(e.source != e.target for e in edges)  # no self-loop from the collapse bug


def test_walk_node_label_sets_key_and_title():
    """A per-instance `node_label` becomes the node's key AND display title (how labs names DAG boxes)."""

    @dataclass(frozen=True)
    class Labelled(Step[Result]):
        """A step that carries a per-instance node label."""

        node_label: str = ""

        def run(self, x: int) -> Result:
            return Result(value=x)

    a = Labelled(node_label="cluster")(x=InputParam[int]("x"))
    nodes, _ = walk(a)
    box = next(n for n in nodes if n.kind == "step")
    assert box.name == "cluster" and box.title == "cluster"


# render_pipeline_svg — pipeline-from-disk rendering


def _write_v2_binary_pipeline(pipeline_dir: Path) -> None:
    """Write a minimal v2 binary pipeline (pipeline.json + per-step step.json)."""
    import json

    steps: list[tuple[str, str, dict[str, object]]] = [
        ("00_tokenizer", "adag/tokenizer/v1", {"model_name": "bert", "max_length": 512}),
        ("01_huggingface_encoder", "adag/huggingface_encoder/v1", {"model_name": "bert"}),
        (
            "02_linear_head",
            "adag/linear_head/v1",
            {"embedding_dim": 768, "positive_label": "pos", "negative_label": "neg"},
        ),
        ("03_binary_selector", "adag/binary_selector/v1", {}),
    ]
    step_dirs = [name for name, _, _ in steps]
    edges = [
        {"source": "00_tokenizer.output", "target": "01_huggingface_encoder.input"},
        {"source": "01_huggingface_encoder.output", "target": "02_linear_head.input"},
        {"source": "02_linear_head.output", "target": "03_binary_selector.input"},
    ]
    pipeline_json: dict[str, object] = {
        "kind": "adag/pipeline/binary/v2",
        "steps": step_dirs,
        "edges": edges,
        "parameters": {"text": {"type": "str"}},
        "output": "03_binary_selector",
    }
    (pipeline_dir / "pipeline.json").write_text(json.dumps(pipeline_json))

    for name, kind, spec in steps:
        step_dir = pipeline_dir / name
        step_dir.mkdir()
        (step_dir / "step.json").write_text(json.dumps({"kind": kind, "spec": spec}))


def test_render_pipeline_svg_binary_pipeline(tmp_path: Path) -> None:
    """Render a v2 binary pipeline directory as SVG."""
    from adag.visualization import render_pipeline_svg

    pipeline_dir = tmp_path / "pipeline"
    pipeline_dir.mkdir()
    _write_v2_binary_pipeline(pipeline_dir)

    result = render_pipeline_svg(pipeline_dir)

    assert result is not None
    assert "<svg" in result
    assert "Tokenize" in result


def test_render_pipeline_svg_no_steps_returns_none(tmp_path: Path) -> None:
    """A pipeline.json with an empty steps list returns None."""
    import json

    from adag.visualization import render_pipeline_svg

    pipeline_dir = tmp_path / "pipeline"
    pipeline_dir.mkdir()
    (pipeline_dir / "pipeline.json").write_text(
        json.dumps({"kind": "adag/pipeline/binary/v2", "steps": [], "edges": []})
    )

    assert render_pipeline_svg(pipeline_dir) is None


def test_render_pipeline_svg_no_pipeline_json_returns_none(tmp_path: Path) -> None:
    """Missing pipeline.json returns None."""
    from adag.visualization import render_pipeline_svg

    pipeline_dir = tmp_path / "pipeline"
    pipeline_dir.mkdir()

    assert render_pipeline_svg(pipeline_dir) is None


def test_edge_tail_draws_full_curve_to_arrow_base_without_kink() -> None:
    """The tail keeps the full quintic and ends at the arrow base perpendicular.

    Regression: the old code truncated the curve above the arrow and inserted a
    straight stub, which left a visible kink where the diagonal met the vertical.
    The quintic already arrives vertical, so the whole curve is drawn as-is.
    """
    points = [(100.0, 10.0), (105.0, 20.0), (108.0, 30.0), (109.5, 35.0), (110.0, 40.0)]
    parts = _edge_tail_parts(points, x2=110.0, arrow_base_y=40.0)

    assert "Q" not in " ".join(parts)  # no fillet
    assert len(parts) == len(points)  # every routed point kept (no truncation)
    assert parts[0] == "M 100.0,10.0"
    assert parts[-1] == "L 110.0,40.0"  # ends exactly on the arrow base


def test_render_pipeline_svg_carries_classes_not_colours(tmp_path: Path) -> None:
    """Colours live in the embedded stylesheet; the drawing itself is only classed.

    That is what lets a host page retheme the graph — it can override the classes,
    or drop the stylesheet entirely and supply its own.
    """
    import re

    from adag.visualization import render_pipeline_svg

    pipeline_dir = tmp_path / "pipeline"
    pipeline_dir.mkdir()
    _write_v2_binary_pipeline(pipeline_dir)

    result = render_pipeline_svg(pipeline_dir)
    assert result is not None

    stylesheet, drawing = result.split("</style>", 1)
    assert "dag-node--step" in drawing
    assert "dag-edge" in drawing
    assert re.search(r"#[0-9a-fA-F]{6}", drawing) is None
    assert re.search(r"#[0-9a-fA-F]{6}", stylesheet) is not None
    # An intrinsic size to render at, and a viewBox so a host can scale it down.
    assert 'viewBox="' in result
    assert re.match(r"<svg[^>]*\swidth=", result) is not None


def test_render_pipeline_svg_options_drop_styles_background_and_params(tmp_path: Path) -> None:
    """Each option removes exactly what it names, and dropping params shrinks the canvas."""
    import re

    from adag.visualization import SvgOptions, render_pipeline_svg

    pipeline_dir = tmp_path / "pipeline"
    pipeline_dir.mkdir()
    _write_v2_binary_pipeline(pipeline_dir)

    def canvas_height(svg: str) -> float:
        match = re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', svg)
        assert match is not None
        return float(match.group(1))

    default = render_pipeline_svg(pipeline_dir)
    bare = render_pipeline_svg(
        pipeline_dir, options=SvgOptions(include_styles=False, include_background=False)
    )
    compact = render_pipeline_svg(pipeline_dir, options=SvgOptions(show_params=False))
    dark = render_pipeline_svg(pipeline_dir, options=SvgOptions(theme="dark"))
    assert default is not None and bare is not None and compact is not None and dark is not None

    assert "<style" not in bare
    assert "dag-canvas" not in bare
    assert "dag-node--step" in bare  # classes survive; only the paint is gone

    # Params are stripped before layout, so the boxes shrink with the content.
    assert canvas_height(compact) < canvas_height(default)

    assert dark != default
