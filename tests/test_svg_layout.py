"""Structural tests for the SVG that ``dag`` and ``render_graph_svg`` draw.

They parse the SVG and check geometry (boxes, edges, text sizes), never exact coordinates. Each
graph in ``tests/graphs_fixtures.py`` is drawn in every style, input mode and density. To cover a
new drawing bug, add a graph there.
"""

import itertools
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

from adag.svg_paint import Density, Inputs, Style
from adag.svg_theme import stylesheet
from adag.visualization import NodeMetadata, SvgOptions, render_graph_svg
from graphs_fixtures import GRAPHS, GraphFixture

SVG_NS = "{http://www.w3.org/2000/svg}"
DOCS_ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"
MIN_FONT = {"blueprint": 11, "band": 11, "classic": 9}
ARROW = 10  # the arrowhead is 8 or 9 px tall; a box top within this of an edge end is its target
SAMPLES = 8
MAX_ASPECT = 3.0
MAX_SPREAD = {"normal": 8.0, "compact": 16.0}  # canvas area / sum of box areas

STYLES: tuple[Style, ...] = ("blueprint", "band", "classic")
INPUTS: tuple[Inputs, ...] = ("terminals", "tabs", "rows")
DENSITIES: tuple[Density, ...] = ("normal", "compact")
Combo = tuple[Style, Inputs, Density]
COMBOS: list[Combo] = list(itertools.product(STYLES, INPUTS, DENSITIES))
BIG = ("namecheck", "namecheck_big")

Point = tuple[float, float]


@dataclass(frozen=True)
class Box:
    """The bounding box of one node shape."""

    left: float
    top: float
    right: float
    bottom: float
    kind: str = ""
    """The ``data-kind`` of its group: ``param`` or ``file`` for an input terminal, ``step``, ..."""

    def overlaps(self, other: "Box") -> bool:
        return (
            self.left < other.right
            and other.left < self.right
            and self.top < other.bottom
            and other.top < self.bottom
        )

    def contains(self, point: Point, margin: float = 0.0) -> bool:
        x, y = point
        return (
            self.left + margin < x < self.right - margin
            and self.top + margin < y < self.bottom - margin
        )

    @property
    def area(self) -> float:
        return (self.right - self.left) * (self.bottom - self.top)


@dataclass(frozen=True)
class Drawing:
    """What the tests read out of an SVG: node boxes, edge polylines, text sizes."""

    width: float
    height: float
    boxes: tuple[Box, ...]
    edges: tuple[tuple[Point, ...], ...]
    font_sizes: tuple[float, ...]


def _cubic(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    u = 1 - t
    return (
        u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
        u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
    )


def _quad(p0: Point, p1: Point, p2: Point, t: float) -> Point:
    u = 1 - t
    return (
        u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
        u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
    )


def path_points(d: str) -> tuple[Point, ...]:
    """Points along an ``M``, ``L``, ``C``, ``Q`` path, with each curve sampled."""
    tokens = re.findall(r"[MLCQ]|-?\d+(?:\.\d+)?", d)
    points: list[Point] = []
    current: Point = (0.0, 0.0)
    i = 0
    while i < len(tokens):
        command = tokens[i]
        i += 1
        count = {"M": 1, "L": 1, "C": 3, "Q": 2}[command]
        coords = [(float(tokens[i + 2 * k]), float(tokens[i + 2 * k + 1])) for k in range(count)]
        i += 2 * count
        if command in "ML":
            current = coords[0]
            points.append(current)
        elif command == "C":
            points.extend(
                _cubic(current, coords[0], coords[1], coords[2], t / SAMPLES)
                for t in range(1, SAMPLES + 1)
            )
            current = coords[2]
        else:
            points.extend(
                _quad(current, coords[0], coords[1], t / SAMPLES) for t in range(1, SAMPLES + 1)
            )
            current = coords[1]
    return tuple(points)


def read_drawing(svg: str) -> Drawing:
    root = ET.fromstring(svg)  # noqa: S314
    view_box = [float(n) for n in root.get("viewBox", "0 0 0 0").split()]
    boxes: list[Box] = []
    edges: list[tuple[Point, ...]] = []
    sizes: list[float] = []
    kinds: dict[ET.Element, str] = {}
    for group in root.iter(f"{SVG_NS}g"):
        for child in group:
            kinds[child] = group.get("data-kind", "")
    for element in root.iter():
        tag = element.tag.removeprefix(SVG_NS)
        classes = set(element.get("class", "").split())
        kind = kinds.get(element, "")
        if tag == "rect" and "dag-node" in classes:
            x, y = float(element.get("x", 0)), float(element.get("y", 0))
            w, h = float(element.get("width", 0)), float(element.get("height", 0))
            boxes.append(Box(x, y, x + w, y + h, kind))
        elif tag == "ellipse" and "dag-node" in classes:
            cx, cy = float(element.get("cx", 0)), float(element.get("cy", 0))
            rx, ry = float(element.get("rx", 0)), float(element.get("ry", 0))
            boxes.append(Box(cx - rx, cy - ry, cx + rx, cy + ry, kind))
        elif tag == "path" and "dag-edge" in classes:
            edges.append(path_points(element.get("d", "")))
        elif tag == "text" and "font-size" in element.attrib:
            sizes.append(float(element.get("font-size", 0)))
    return Drawing(view_box[2], view_box[3], tuple(boxes), tuple(edges), tuple(sizes))


def _ends(points: tuple[Point, ...], boxes: tuple[Box, ...]) -> tuple[int | None, int | None]:
    """Indexes of the source and target box of an edge, found from where it starts and ends."""
    start, end = points[0], points[-1]
    source = target = None
    for index, box in enumerate(boxes):
        if box.left - 1 <= start[0] <= box.right + 1 and abs(box.bottom - start[1]) < 2:
            source = index
        if box.left - 1 <= end[0] <= box.right + 1 and 0 <= box.top - end[1] <= ARROW:
            target = index
    return source, target


def edge_under_a_box(drawing: Drawing) -> bool:
    """True when an edge has a point inside a box that is not one of its two ends."""
    for points in drawing.edges:
        source, target = _ends(points, drawing.boxes)
        for index, box in enumerate(drawing.boxes):
            if index in (source, target):
                continue
            if any(box.contains(p, margin=1.0) for p in points):
                return True
    return False


def _segments_cross(a: tuple[Point, Point], b: tuple[Point, Point]) -> bool:
    def side(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2 = side(*b, a[0]), side(*b, a[1])
    d3, d4 = side(*a, b[0]), side(*a, b[1])
    return d1 * d2 < 0 and d3 * d4 < 0


def crossings(drawing: Drawing) -> int:
    """Pairs of edges that cross. Edges that share a source or a target never count."""
    ends = [_ends(points, drawing.boxes) for points in drawing.edges]
    count = 0
    for i, j in itertools.combinations(range(len(drawing.edges)), 2):
        (s1, t1), (s2, t2) = ends[i], ends[j]
        if (s1 is not None and s1 == s2) or (t1 is not None and t1 == t2):
            continue
        a = list(itertools.pairwise(drawing.edges[i]))
        b = list(itertools.pairwise(drawing.edges[j]))
        if any(_segments_cross(p, q) for p in a for q in b):
            count += 1
    return count


def render(
    graph: GraphFixture, style: Style, inputs: Inputs, density: Density, theme: str = "light"
) -> str:
    return render_graph_svg(
        list(graph.nodes),
        list(graph.edges),
        output_label=graph.output_label,
        final_step_name=graph.final,
        options=SvgOptions(theme=theme, style=style, inputs=inputs, density=density),
    )


def _id(combo: Combo) -> str:
    return "-".join(combo)


# The crossings of the first good render of each big graph (terminals in the top row), per style, input mode and density.
# A change that adds more than two is a regression in the layout.
CROSSING_BASELINE: dict[str, int] = {
    "namecheck/blueprint-terminals-normal": 9,
    "namecheck/blueprint-terminals-compact": 6,
    "namecheck/blueprint-tabs-normal": 0,
    "namecheck/blueprint-tabs-compact": 0,
    "namecheck/blueprint-rows-normal": 0,
    "namecheck/blueprint-rows-compact": 0,
    "namecheck/band-terminals-normal": 9,
    "namecheck/band-terminals-compact": 6,
    "namecheck/band-tabs-normal": 0,
    "namecheck/band-tabs-compact": 0,
    "namecheck/band-rows-normal": 0,
    "namecheck/band-rows-compact": 0,
    "namecheck/classic-terminals-normal": 9,
    "namecheck/classic-terminals-compact": 6,
    "namecheck/classic-tabs-normal": 7,
    "namecheck/classic-tabs-compact": 7,
    "namecheck/classic-rows-normal": 7,
    "namecheck/classic-rows-compact": 7,
    "namecheck_big/blueprint-terminals-normal": 8,
    "namecheck_big/blueprint-terminals-compact": 9,
    "namecheck_big/blueprint-tabs-normal": 0,
    "namecheck_big/blueprint-tabs-compact": 0,
    "namecheck_big/blueprint-rows-normal": 0,
    "namecheck_big/blueprint-rows-compact": 0,
    "namecheck_big/band-terminals-normal": 8,
    "namecheck_big/band-terminals-compact": 9,
    "namecheck_big/band-tabs-normal": 0,
    "namecheck_big/band-tabs-compact": 0,
    "namecheck_big/band-rows-normal": 0,
    "namecheck_big/band-rows-compact": 0,
    "namecheck_big/classic-terminals-normal": 10,
    "namecheck_big/classic-terminals-compact": 9,
    "namecheck_big/classic-tabs-normal": 8,
    "namecheck_big/classic-tabs-compact": 8,
    "namecheck_big/classic-rows-normal": 8,
    "namecheck_big/classic-rows-compact": 8,
}


# Corners of the first good render (the per-edge router), plus a small margin. Blueprint counts the
# rounded corners of every edge, band counts the curves (two per sideways move).
BEND_BASELINE: dict[str, int] = {
    "ml_training/blueprint-normal": 33,
    "ml_training/blueprint-compact": 27,
    "ml_training/band-normal": 18,
    "ml_training/band-compact": 16,
    "etl_fan_in/blueprint-normal": 35,
    "etl_fan_in/blueprint-compact": 41,
    "etl_fan_in/band-normal": 18,
    "etl_fan_in/band-compact": 18,
    "layered_40/blueprint-normal": 152,
    "layered_40/blueprint-compact": 154,
    "layered_40/band-normal": 78,
    "layered_40/band-compact": 77,
    "namecheck/blueprint-normal": 41,
    "namecheck/blueprint-compact": 39,
    "namecheck/band-normal": 21,
    "namecheck/band-compact": 20,
    "namecheck_big/blueprint-normal": 47,
    "namecheck_big/blueprint-compact": 47,
    "namecheck_big/band-normal": 24,
    "namecheck_big/band-compact": 24,
}


def _bends_per_edge(svg: str, style: Style) -> list[int]:
    """The corners of each edge: ``Q`` commands in blueprint, ``C`` commands in band."""
    mark = "Q" if style == "blueprint" else "C"
    return [
        p.get("d", "").count(mark)
        for p in ET.fromstring(svg).iter(f"{SVG_NS}path")  # noqa: S314
        if "dag-edge" in p.get("class", "").split()
    ]


@pytest.mark.parametrize("graph", sorted(GRAPHS))
@pytest.mark.parametrize("style", ["blueprint", "band"])
@pytest.mark.parametrize("density", DENSITIES)
def test_no_edge_bends_more_than_a_blocked_z_does(
    graph: str, style: Style, density: Density
) -> None:
    """An edge has at most one sideways move, or two when a box blocks the plain one."""
    bends = _bends_per_edge(render(GRAPHS[graph], style, "terminals", density), style)
    assert max(bends, default=0) <= (4 if style == "blueprint" else 2), bends


@pytest.mark.parametrize("graph", ["ml_training", "etl_fan_in", "layered_40", *BIG])
@pytest.mark.parametrize("style", ["blueprint", "band"])
@pytest.mark.parametrize("density", DENSITIES)
def test_total_bends_stay_near_the_first_good_render(
    graph: str, style: Style, density: Density
) -> None:
    total = sum(_bends_per_edge(render(GRAPHS[graph], style, "terminals", density), style))
    assert total <= BEND_BASELINE[f"{graph}/{style}-{density}"], total


@pytest.mark.parametrize("graph", sorted(GRAPHS))
@pytest.mark.parametrize("combo", COMBOS, ids=_id)
def test_every_graph_draws_well(graph: str, combo: Combo) -> None:
    style = combo[0]
    svg = render(GRAPHS[graph], *combo)
    drawing = read_drawing(svg)  # well-formed XML, or this raises

    for first, second in itertools.combinations(drawing.boxes, 2):
        assert not first.overlaps(second), (first, second)
    for box in drawing.boxes:
        assert box.left >= 0 and box.top >= 0, box
        assert box.right <= drawing.width and box.bottom <= drawing.height, box
    for points in drawing.edges:
        for x, y in points:
            assert -0.5 <= x <= drawing.width + 0.5 and -0.5 <= y <= drawing.height + 0.5
    assert not edge_under_a_box(drawing)
    assert min(drawing.font_sizes) >= MIN_FONT[style]
    assert ET.fromstring(svg).tag == f"{SVG_NS}svg"  # noqa: S314


@pytest.mark.parametrize("graph", BIG)
@pytest.mark.parametrize("combo", COMBOS, ids=_id)
def test_big_graphs_stay_compact_and_uncrossed(graph: str, combo: Combo) -> None:
    drawing = read_drawing(render(GRAPHS[graph], *combo))
    area = drawing.width * drawing.height
    assert area <= MAX_SPREAD[combo[2]] * sum(box.area for box in drawing.boxes)
    aspect = max(drawing.width / drawing.height, drawing.height / drawing.width)
    assert aspect <= MAX_ASPECT
    baseline = CROSSING_BASELINE.get(f"{graph}/{_id(combo)}")
    assert baseline is not None, (
        f"{graph}/{_id(combo)}: {crossings(drawing)} crossings, no baseline"
    )
    assert crossings(drawing) <= baseline + 2


def test_a_graph_of_40_nodes_and_80_edges_draws_in_two_seconds() -> None:
    nodes = [
        NodeMetadata(
            name=f"n{level}_{i}", kind="step", title=f"Step {level}.{i}", description="A step."
        )
        for level in range(8)
        for i in range(5)
    ]
    edges: list[tuple[str, str]] = []
    for level in range(7):
        for i in range(5):
            edges.append((f"n{level}_{i}", f"n{level + 1}_{i}"))
            edges.append((f"n{level}_{i}", f"n{level + 1}_{(i + 2) % 5}"))
    assert len(nodes) == 40 and len(edges) == 70
    edges += [(f"n0_{i}", f"n{3 + i}_{i}") for i in range(5)] + [
        (f"n1_{i}", "n7_0") for i in range(5)
    ]
    for style in ("blueprint", "band"):
        started = time.perf_counter()
        svg = render_graph_svg(nodes, edges, options=SvgOptions(style=style))
        assert time.perf_counter() - started < 2.0
        drawing = read_drawing(svg)
        assert not any(a.overlaps(b) for a, b in itertools.combinations(drawing.boxes, 2))


@pytest.mark.parametrize("graph", sorted(GRAPHS))
@pytest.mark.parametrize("combo", COMBOS, ids=_id)
def test_input_terminals_share_the_top_row_above_every_step(graph: str, combo: Combo) -> None:
    drawing = read_drawing(render(GRAPHS[graph], *combo))
    terminals = [b for b in drawing.boxes if b.kind in ("param", "file")]
    others = [b for b in drawing.boxes if b.kind not in ("param", "file")]
    if not terminals:
        return
    centres = {round((b.top + b.bottom) / 2, 1) for b in terminals}
    assert len(centres) == 1, centres  # one row
    assert max(b.bottom for b in terminals) < min(b.top for b in others)  # a clear gap below it
    assert min(b.top for b in drawing.boxes) == min(b.top for b in terminals)


def _expected_counts(graph: GraphFixture, combo: Combo) -> tuple[int, int]:
    """How many node groups and edge paths a drawing has: inputs are folded away or drawn."""
    style, inputs, _ = combo
    folded = style != "classic" and inputs != "terminals"
    names = {n.name: n for n in graph.nodes}
    foldable = {
        n.name
        for n in graph.nodes
        if n.kind in ("param", "file")
        and all(names[t].kind == "step" for s, t in graph.edges if s == n.name)
    }
    drawn = [n for n in graph.nodes if not (folded and n.name in foldable)]
    edges = [(a, b) for a, b in graph.edges if not (folded and a in foldable)]
    has_output = bool(graph.output_label) and graph.final is not None
    return len(drawn) + int(has_output), len(edges) + int(has_output)


@pytest.mark.parametrize("graph", sorted(GRAPHS))
@pytest.mark.parametrize("combo", COMBOS, ids=_id)
def test_every_node_and_every_edge_is_drawn_once(graph: str, combo: Combo) -> None:
    root = ET.fromstring(render(GRAPHS[graph], *combo))  # noqa: S314 (adag's own output)
    groups = [g for g in root.iter(f"{SVG_NS}g") if "dag-item" in g.get("class", "").split()]
    paths = [p for p in root.iter(f"{SVG_NS}path") if "dag-edge" in p.get("class", "").split()]
    assert (len(groups), len(paths)) == _expected_counts(GRAPHS[graph], combo)


@pytest.mark.parametrize("graph", sorted(GRAPHS))
@pytest.mark.parametrize("combo", COMBOS, ids=_id)
def test_every_text_is_inside_the_canvas(graph: str, combo: Combo) -> None:
    root = ET.fromstring(render(GRAPHS[graph], *combo))  # noqa: S314 (adag's own output)
    width, height = (float(v) for v in root.get("viewBox", "0 0 0 0").split()[2:])
    for text in root.iter(f"{SVG_NS}text"):
        size = float(text.get("font-size", 0))
        reach = len(text.text or "") * size * 0.62
        x, y = float(text.get("x", 0)), float(text.get("y", 0))
        anchor = text.get("text-anchor", "start")
        left = {"start": x, "middle": x - reach / 2, "end": x - reach}[anchor]
        assert left >= -1 and left + reach <= width + 1, (text.text, left, reach, width)
        assert y - size / 2 >= 0 and y + size / 2 <= height, (text.text, y, height)


Segment = tuple[float, float, float, float]


def straight_segments(d: str) -> list[Segment]:
    """The straight runs (``L``) of a right-angle edge, without its rounded bends."""
    tokens = re.findall(r"[MLQ]|-?\d+(?:\.\d+)?", d)
    segments: list[Segment] = []
    current = (0.0, 0.0)
    i = 0
    while i < len(tokens):
        command = tokens[i]
        count = 2 if command == "Q" else 1
        points = [
            (float(tokens[i + 1 + 2 * k]), float(tokens[i + 2 + 2 * k])) for k in range(count)
        ]
        i += 1 + 2 * count
        if command == "L":
            segments.append((*current, *points[0]))
        current = points[-1]
    return segments


MIN_LANE_GAP = 4.0
MIN_BOX_GAP = 6.0


@pytest.mark.parametrize("graph", ["namecheck", "namecheck_big"])
def test_lanes_keep_apart_from_each_other_and_from_boxes(graph: str) -> None:
    """In blueprint, parallel lanes are 4 px apart (unless they leave one source) and 6 px from a box."""
    root = ET.fromstring(render(GRAPHS[graph], "blueprint", "terminals", "normal"))  # noqa: S314
    drawing = read_drawing(ET.tostring(root, encoding="unicode"))
    edges = [
        straight_segments(p.get("d", ""))
        for p in root.iter(f"{SVG_NS}path")
        if "dag-edge" in p.get("class", "").split()
    ]
    for (i, first), (j, second) in itertools.combinations(enumerate(edges), 2):
        same_source = (
            _ends(drawing.edges[i], drawing.boxes)[0] == _ends(drawing.edges[j], drawing.boxes)[0]
        )
        for ax1, ay1, ax2, ay2 in first:
            for bx1, by1, bx2, by2 in second:
                if ax1 == ax2 and bx1 == bx2:  # two vertical lanes that share some height
                    shared = min(max(ay1, ay2), max(by1, by2)) - max(min(ay1, ay2), min(by1, by2))
                    if shared > 2 and not (same_source and ax1 == bx1):
                        assert abs(ax1 - bx1) >= MIN_LANE_GAP, (i, j, ax1, bx1)
                if ay1 == ay2 and by1 == by2:  # two horizontal runs that share some width
                    shared = min(max(ax1, ax2), max(bx1, bx2)) - max(min(ax1, ax2), min(bx1, bx2))
                    if shared > 2 and not (same_source and ay1 == by1):
                        assert abs(ay1 - by1) >= MIN_LANE_GAP, (i, j, ay1, by1)
    for points, segments in zip(drawing.edges, edges, strict=True):
        source, target = _ends(points, drawing.boxes)
        for index, box in enumerate(drawing.boxes):
            if index in (source, target):
                continue
            for x1, y1, x2, y2 in segments:
                if x1 == x2 and min(y1, y2) < box.bottom and max(y1, y2) > box.top:
                    assert x1 <= box.left - MIN_BOX_GAP or x1 >= box.right + MIN_BOX_GAP, (x1, box)
                if y1 == y2 and min(x1, x2) < box.right and max(x1, x2) > box.left:
                    assert y1 <= box.top - MIN_BOX_GAP or y1 >= box.bottom + MIN_BOX_GAP, (y1, box)


def _selectors(css: str) -> set[str]:
    return {s.strip() for s in re.findall(r"([^{}]+)\{", css)}


def test_light_and_dark_stylesheets_style_the_same_selectors() -> None:
    assert _selectors(stylesheet("light")) == _selectors(stylesheet("dark"))


@pytest.mark.parametrize("name", ["graph.svg", "graph_states.svg"])
def test_the_docs_images_are_drawn_well(name: str) -> None:
    drawing = read_drawing((DOCS_ASSETS / name).read_text())
    for first, second in itertools.combinations(drawing.boxes, 2):
        assert not first.overlaps(second)
    assert min(drawing.font_sizes) >= MIN_FONT["blueprint"]
    assert not edge_under_a_box(drawing)
