"""Tests for DAG visualization — walk, nodes, edges."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import InputParam, Step
from adag.io import FileSource
from adag.visualization import walk


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
    nodes, _edges = walk(result)
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


def test_every_edge_arrives_vertically_at_the_arrow_base() -> None:
    """An edge ends on the base of its arrowhead, moving straight down, in every style.

    Regression: an early router cut the curve above the arrow and added a stub, which left a kink
    where the diagonal met the vertical.
    """
    import re
    import xml.etree.ElementTree as ET

    from adag.visualization import NodeMetadata, SvgOptions, render_graph_svg

    nodes = [NodeMetadata(name=n, kind="step", title=n) for n in ("a", "b", "c", "d")]
    edges = [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"), ("a", "d")]
    for style in ("blueprint", "band", "classic"):
        svg = render_graph_svg(nodes, edges, options=SvgOptions(style=style))
        root = ET.fromstring(svg)  # noqa: S314 (adag's own output)
        ns = "{http://www.w3.org/2000/svg}"
        paths = [e for e in root.iter(f"{ns}path") if "dag-edge" in e.get("class", "")]
        arrows = list(root.iter(f"{ns}polygon"))
        assert len(paths) == len(arrows) == len(edges)
        for path, arrow in zip(paths, arrows, strict=True):
            numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", path.get("d", ""))]
            end_x, end_y = numbers[-2], numbers[-1]
            tip = [
                tuple(float(v) for v in pt.split(",")) for pt in arrow.get("points", "").split()
            ][2]
            assert end_x == tip[0] and end_y < tip[1]  # on the arrow base, straight above the tip
            assert numbers[-4] == end_x  # the last control point or the previous point: same x


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


def test_dag_helper_renders_inline_in_a_notebook() -> None:
    """``dag(output)`` returns an object Jupyter renders through ``_repr_svg_``."""
    from adag import dag as dag_helper

    svg = dag_helper(MiddleStep()(source=LeafStep()(x=3)))
    assert svg._repr_svg_().startswith("<svg")  # pyright: ignore[reportPrivateUsage]


def test_render_graph_svg_is_public_and_old_name_still_works() -> None:
    from adag.visualization import NodeMetadata, _render_graph_svg, render_graph_svg

    nodes = [
        NodeMetadata(name="a", kind="step", title="A"),
        NodeMetadata(name="b", kind="step", title="B"),
    ]
    assert render_graph_svg(nodes, [("a", "b")]).startswith("<svg")
    assert _render_graph_svg is render_graph_svg


def test_compute_state_sees_a_pipeline_cached_node(tmp_path: Path) -> None:
    """A node cached by a pipeline run shows CACHED, read from its node key."""
    from adag import Pipeline
    from adag.visualization import compute_state, walk

    output = MiddleStep()(source=LeafStep()(x=3))
    nodes, _ = walk(output)
    assert {compute_state(n, tmp_path) for n in nodes if n.step is not None} == {"PENDING"}
    Pipeline(output, cache_dir=tmp_path).execute()
    assert {compute_state(n, tmp_path) for n in nodes if n.step is not None} == {"CACHED"}


def test_custom_rendering_through_public_names() -> None:
    """Walk, replace one node's metadata, render: the new title is in the SVG."""
    import dataclasses

    from adag import node_metadata, render_graph_svg, walk

    nodes, edges = walk(MiddleStep()(source=LeafStep()(x=InputParam[int]("x"))))
    metadata = node_metadata(nodes)
    assert [m.name for m in metadata] == [n.name for n in nodes]
    changed = [
        dataclasses.replace(m, title="My custom title") if m.kind == "step" else m for m in metadata
    ]
    svg = render_graph_svg(changed, [(e.source, e.target) for e in edges])
    assert "My custom title" in svg


def test_dag_colors_cached_steps_apart_from_pending_steps(tmp_path: Path) -> None:
    """A cached step has its own fill in both themes, so the state shows without custom CSS."""
    from dataclasses import dataclass

    from pydantic import BaseModel

    from adag import NullObserver, Pipeline, Step, dag
    from adag.visualization import SvgOptions

    class Num(BaseModel):
        value: int

    @dataclass(frozen=True)
    class Base(Step[Num]):
        def run(self) -> Num:
            return Num(value=1)

    @dataclass(frozen=True)
    class Top(Step[Num]):
        bump: int = 0

        def run(self, base: Num, bump: int) -> Num:
            return Num(value=base.value + bump)

    cache = tmp_path / "cache"
    Pipeline(Top()(base=Base()()), cache_dir=cache).execute(observer=NullObserver())
    changed = Top(bump=1)(base=Base()())

    for theme in ("light", "dark"):
        svg = str(dag(changed, cache_dir=cache, options=SvgOptions(theme=theme)))
        stylesheet, drawing = svg.split("</style>", 1)
        assert "is-cached" in stylesheet
        assert "is-cached" in drawing  # Base kept its result
        assert "is-pending" in drawing  # Top has a new field


def test_a_step_with_no_docstring_has_no_description() -> None:
    """``dataclass`` writes "Name(field: int = 1)" as the docstring: it is not a description."""
    from adag.visualization import node_metadata

    @dataclass(frozen=True)
    class Undocumented(Step[Result]):
        case: str = "lower"

        def run(self, x: int) -> Result:
            return Result(value=x)

    nodes, _ = walk(Undocumented()(x=InputParam[int]("x")))
    meta = next(m for m in node_metadata(nodes) if m.kind == "step")
    assert meta.description == ""


def test_a_documented_step_keeps_its_first_docstring_line() -> None:
    from adag.visualization import node_metadata

    nodes, _ = walk(LeafStep()(x=InputParam[int]("x")))
    meta = next(m for m in node_metadata(nodes) if m.kind == "step")
    assert meta.description == "Leaf step with no upstream"


def test_a_labelled_final_step_keeps_the_edge_to_the_output() -> None:
    """The output edge uses the name walk() gave the node, not the class name."""
    from adag import dag

    @dataclass(frozen=True)
    class Labelled(Step[Result]):
        """A step with a per-instance label."""

        node_label: str = ""

        def run(self, x: int) -> Result:
            return Result(value=x)

    labelled = Labelled(node_label="cluster")(x=InputParam[int]("x"))
    plain = LeafStep()(x=InputParam[int]("x"))
    # Two edges into the output box means two edge paths ending at it.
    assert str(dag(labelled)).count("<path") == str(dag(plain)).count("<path")
