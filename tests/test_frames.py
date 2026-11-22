"""Frames: the body of a fan-out or a loop drawn once inside a dashed box.

Structural tests: they parse the SVG and check geometry, never exact coordinates. The graphs are
``FRAME_GRAPHS`` in ``tests/graphs_fixtures.py``, drawn in blueprint and band, every input mode,
both densities and both corner shapes.
"""

import itertools
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import pytest

from adag.svg_paint import Corners, Density, Inputs
from adag.visualization import SvgOptions, render_graph_svg
from graphs_fixtures import FRAME_GRAPHS, GraphFixture
from test_svg_layout import Box, path_points

NS = "{http://www.w3.org/2000/svg}"
TOLERANCE = 0.6

COMBOS: list[tuple[str, Inputs, Density, Corners]] = [
    (style, inputs, density, corners)
    for style in ("blueprint", "band")
    for inputs, density, corners in itertools.product(
        ("terminals", "tabs", "rows"), ("normal", "compact"), ("round", "sharp")
    )
]
CASES = [(g, *c) for g in sorted(FRAME_GRAPHS) for c in COMBOS]


def render(
    graph: GraphFixture, style: str, inputs: Inputs, density: Density, corners: Corners
) -> str:
    return render_graph_svg(
        list(graph.nodes),
        list(graph.edges),
        output_label=graph.output_label,
        final_step_name=graph.final,
        options=SvgOptions(style=style, inputs=inputs, density=density, corners=corners),  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class Item:
    name: str
    kind: str
    parent: str
    state: str
    box: Box


@dataclass(frozen=True)
class Frame:
    name: str
    parent: str
    shape: str
    box: Box
    tag: tuple[float, float]
    caption: tuple[float, float] | None


def _box_of(group: ET.Element, kind: str = "") -> Box | None:
    for element in group:
        classes = set(element.get("class", "").split())
        tag = element.tag.removeprefix(NS)
        if tag == "rect" and "dag-node" in classes:
            x, y = float(element.get("x", 0)), float(element.get("y", 0))
            return Box(
                x, y, x + float(element.get("width", 0)), y + float(element.get("height", 0)), kind
            )
    return None


def read(svg: str) -> tuple[list[Item], list[Frame], ET.Element]:
    root = ET.fromstring(svg)  # noqa: S314 (adag's own output)
    items: list[Item] = []
    frames: list[Frame] = []
    for group in root.iter(f"{NS}g"):
        kind = group.get("data-kind", "")
        classes = set(group.get("class", "").split())
        if "dag-frame" in classes:
            border = next(e for e in group if "dag-frame__border" in e.get("class", "").split())
            x, y = float(border.get("x", 0)), float(border.get("y", 0))
            w, h = float(border.get("width", 0)), float(border.get("height", 0))
            texts = {
                part: next(
                    (e for e in group if f"dag-frame__{part}" in e.get("class", "").split()), None
                )
                for part in ("tag", "caption")
            }
            tag = texts["tag"]
            caption = texts["caption"]
            frames.append(
                Frame(
                    group.get("data-name", ""),
                    group.get("data-parent", ""),
                    group.get("data-shape", ""),
                    Box(x, y, x + w, y + h, "frame"),
                    (float(tag.get("x", 0)), float(tag.get("y", 0))) if tag is not None else (0, 0),
                    (float(caption.get("x", 0)), float(caption.get("y", 0)))
                    if caption is not None
                    else None,
                )
            )
        elif "dag-item" in classes and kind in (
            "step",
            "template_input",
            "param",
            "file",
            "output",
        ):
            box = _box_of(group, kind)
            if box is not None:
                items.append(
                    Item(
                        group.get("data-name", ""),
                        kind,
                        group.get("data-parent", ""),
                        group.get("data-state", ""),
                        box,
                    )
                )
    return items, frames, root


def _inside(inner: Box, outer: Box) -> bool:
    return (
        inner.left >= outer.left - TOLERANCE
        and inner.top >= outer.top - TOLERANCE
        and inner.right <= outer.right + TOLERANCE
        and inner.bottom <= outer.bottom + TOLERANCE
    )


@pytest.mark.parametrize(("graph", "style", "inputs", "density", "corners"), CASES)
def test_every_child_is_inside_its_frame(
    graph: str, style: str, inputs: Inputs, density: Density, corners: Corners
) -> None:
    items, frames, _ = read(render(FRAME_GRAPHS[graph], style, inputs, density, corners))
    by_name = {f.name: f for f in frames}
    assert by_name  # the frames are drawn
    for item in items:
        if item.parent:
            assert _inside(item.box, by_name[item.parent].box), item.name
    for frame in frames:
        if frame.parent:
            assert _inside(frame.box, by_name[frame.parent].box), frame.name


@pytest.mark.parametrize(("graph", "style", "inputs", "density", "corners"), CASES)
def test_boxes_of_one_level_do_not_overlap(
    graph: str, style: str, inputs: Inputs, density: Density, corners: Corners
) -> None:
    items, frames, _ = read(render(FRAME_GRAPHS[graph], style, inputs, density, corners))
    boxes = [(i.parent, i.name, i.box) for i in items] + [(f.parent, f.name, f.box) for f in frames]
    for (pa, na, a), (pb, nb, b) in itertools.combinations(boxes, 2):
        if pa == pb:
            assert not a.overlaps(b), (na, nb)


@pytest.mark.parametrize(("graph", "style", "inputs", "density", "corners"), CASES)
def test_a_box_outside_a_frame_does_not_touch_it(
    graph: str, style: str, inputs: Inputs, density: Density, corners: Corners
) -> None:
    items, frames, _ = read(render(FRAME_GRAPHS[graph], style, inputs, density, corners))
    for frame in frames:
        for item in items:
            inside = item.parent == frame.name or item.parent.startswith(frame.name + "/")
            if not inside:
                assert not item.box.overlaps(frame.box), (item.name, frame.name)


@pytest.mark.parametrize(("graph", "style", "inputs", "density", "corners"), CASES)
def test_the_tag_and_the_caption_are_inside_the_frame(
    graph: str, style: str, inputs: Inputs, density: Density, corners: Corners
) -> None:
    _, frames, _ = read(render(FRAME_GRAPHS[graph], style, inputs, density, corners))
    for frame in frames:
        assert frame.box.left < frame.tag[0] < frame.box.right
        assert frame.box.top < frame.tag[1] < frame.box.bottom
        if frame.caption is not None:
            assert frame.box.left < frame.caption[0] < frame.box.right
            assert frame.box.top < frame.caption[1] < frame.box.bottom


@pytest.mark.parametrize(("graph", "style", "inputs", "density", "corners"), CASES)
def test_a_step_of_a_template_has_no_state(
    graph: str, style: str, inputs: Inputs, density: Density, corners: Corners
) -> None:
    items, _, _ = read(render(FRAME_GRAPHS[graph], style, inputs, density, corners))
    body = [i for i in items if i.parent and i.kind == "step"]
    assert body
    assert {i.state for i in body} == {"none"}


@pytest.mark.parametrize("graph", sorted(FRAME_GRAPHS))
def test_the_input_terminals_are_the_topmost_boxes_of_their_frame(graph: str) -> None:
    items, _, _ = read(render(FRAME_GRAPHS[graph], "blueprint", "terminals", "normal", "round"))
    for parent in {i.parent for i in items if i.parent}:
        children = [i for i in items if i.parent == parent]
        terminals = [i for i in children if i.kind == "template_input"]
        others = [i for i in children if i.kind != "template_input"]
        assert terminals
        assert max(t.box.top for t in terminals) <= min(o.box.top for o in others) + TOLERANCE


@pytest.mark.parametrize("graph", sorted(FRAME_GRAPHS))
def test_every_arrowhead_ends_on_a_box(graph: str) -> None:
    items, frames, root = read(
        render(FRAME_GRAPHS[graph], "blueprint", "terminals", "normal", "round")
    )
    boxes = [i.box for i in items]
    for polygon in root.iter(f"{NS}polygon"):
        classes = polygon.get("class", "").split()
        if "dag-arrow--feedback" in classes:
            continue
        points = [tuple(float(v) for v in p.split(",")) for p in polygon.get("points", "").split()]
        tip = points[-1]
        touches = any(
            (
                abs(tip[1] - b.top) < 1.5
                or abs(tip[1] - b.bottom) < 1.5
                or abs(tip[0] - b.left) < 1.5
                or abs(tip[0] - b.right) < 1.5
            )
            and b.left - 1.5 <= tip[0] <= b.right + 1.5
            and b.top - 1.5 <= tip[1] <= b.bottom + 1.5
            for b in boxes
        ) or any(
            f.box.left <= tip[0] <= f.box.right and abs(tip[1] - f.box.top) < 1.5 for f in frames
        )
        assert touches, tip


def _paths(root: ET.Element, cls: str) -> list[str]:
    return [p.get("d", "") for p in root.iter(f"{NS}path") if cls in p.get("class", "").split()]


@pytest.mark.parametrize("style", ["blueprint", "band"])
def test_the_way_back_of_a_loop_stays_in_its_frame_and_off_its_boxes(style: str) -> None:
    items, frames, root = read(
        render(FRAME_GRAPHS["frame_loop"], style, "terminals", "normal", "round")
    )
    frame = next(f for f in frames if f.shape == "loop")
    back = _paths(root, "dag-edge--feedback")
    assert len(back) == 1
    for x, y in path_points(back[0]):
        assert frame.box.left < x < frame.box.right
        assert frame.box.top < y < frame.box.bottom
        for item in items:
            if item.parent == frame.name:
                assert not item.box.contains((x, y), margin=-1.0) or item.kind == "template_input"


@pytest.mark.parametrize("style", ["blueprint", "band"])
def test_only_a_loop_has_a_way_back(style: str) -> None:
    for name in ("frame_fan_out", "frame_nested"):
        _, _, root = read(render(FRAME_GRAPHS[name], style, "terminals", "normal", "round"))
        assert len(_paths(root, "dag-edge--feedback")) == (1 if name == "frame_nested" else 0)


@pytest.mark.parametrize("graph", sorted(FRAME_GRAPHS))
def test_classic_and_frames_false_draw_one_dashed_box(graph: str) -> None:
    fixture = FRAME_GRAPHS[graph]
    for options in (SvgOptions(style="classic"), SvgOptions(frames=False)):
        svg = render_graph_svg(
            list(fixture.nodes),
            list(fixture.edges),
            output_label=fixture.output_label,
            final_step_name=fixture.final,
            options=options,
        )
        drawing = svg.split("</style>", 1)[1]
        assert "dag-frame" not in drawing
        assert "dag-node--mapped" in drawing or "dag-node--loop" in drawing
        assert 'data-parent="' not in drawing


@pytest.mark.parametrize("graph", sorted(FRAME_GRAPHS))
def test_a_frame_names_its_depth_and_its_shape(graph: str) -> None:
    _, _, root = read(render(FRAME_GRAPHS[graph], "blueprint", "terminals", "normal", "round"))
    depths = {
        g.get("data-name"): g.get("data-depth")
        for g in root.iter(f"{NS}g")
        if "dag-frame" in g.get("class", "").split()
    }
    assert "each" in depths or "halve_until" in depths
    if graph == "frame_nested":
        assert depths["each"] == "1"
        assert depths["each/loop"] == "2"


def test_a_frame_deeper_than_three_levels_is_one_dashed_box() -> None:
    nodes = [
        _frame("f1", "", "mapped"),
        _frame("f1/f2", "f1", "mapped"),
        _frame("f1/f2/f3", "f1/f2", "mapped"),
        _frame("f1/f2/f3/f4", "f1/f2/f3", "mapped"),
    ]
    nodes += [
        _leaf("f1/f2/f3/f4/a", "f1/f2/f3/f4"),
    ]
    svg = render_graph_svg(nodes, [], options=SvgOptions())
    _, frames, _ = read(svg)
    assert [f.name for f in frames] == ["f1", "f1/f2", "f1/f2/f3"]


def _frame(name: str, parent: str, shape: str):  # type: ignore[no-untyped-def]
    from graphs_fixtures import frame_node

    return frame_node(name, name.rsplit("/", 1)[-1], shape, "one branch per item", parent=parent)


def _leaf(name: str, parent: str):  # type: ignore[no-untyped-def]
    from graphs_fixtures import body_step

    return body_step(name, name.rsplit("/", 1)[-1], parent)
