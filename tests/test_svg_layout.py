"""Structural tests for the SVG that ``dag`` and ``render_graph_svg`` draw.

They parse the SVG and check geometry (boxes, edges, text sizes), never exact coordinates or text.
Three graphs are checked: a 12-node graph in the shape of a real consumer's diagram, and the two
images the docs show (``docs/assets``), which are regenerated with ``scripts/gen-docs-graphs``.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

from adag.visualization import (
    _STYLESHEET_DARK,  # pyright: ignore[reportPrivateUsage]
    _STYLESHEET_LIGHT,  # pyright: ignore[reportPrivateUsage]
    NodeMetadata,
    render_graph_svg,
)

SVG_NS = "{http://www.w3.org/2000/svg}"
DOCS_ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"
MIN_FONT_SIZE = 11


@dataclass(frozen=True)
class Box:
    """The bounding box of one node shape."""

    left: float
    top: float
    right: float
    bottom: float

    def overlaps(self, other: "Box") -> bool:
        return (
            self.left < other.right
            and other.left < self.right
            and self.top < other.bottom
            and other.top < self.bottom
        )

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        return (
            self.left + margin < x < self.right - margin
            and self.top + margin < y < self.bottom - margin
        )


@dataclass(frozen=True)
class Drawing:
    """What the tests read out of an SVG: node boxes, edge polylines, text sizes."""

    width: float
    height: float
    boxes: tuple[Box, ...]
    edges: tuple[tuple[tuple[float, float], ...], ...]
    font_sizes: tuple[float, ...]


def _classes(element: ET.Element) -> set[str]:
    return set(element.get("class", "").split())


def _path_points(d: str) -> tuple[tuple[float, float], ...]:
    """Every ``x,y`` pair in a path. Control points of a curve count as points of the edge."""
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]
    return tuple(zip(numbers[0::2], numbers[1::2], strict=False))


def read_drawing(svg: str) -> Drawing:
    root = ET.fromstring(svg)  # noqa: S314 (the SVG is adag's own output)
    view_box = [float(n) for n in root.get("viewBox", "0 0 0 0").split()]
    boxes: list[Box] = []
    edges: list[tuple[tuple[float, float], ...]] = []
    sizes: list[float] = []
    for element in root.iter():
        tag = element.tag.removeprefix(SVG_NS)
        classes = _classes(element)
        if tag == "rect" and "dag-node" in classes:
            x, y = float(element.get("x", 0)), float(element.get("y", 0))
            w, h = float(element.get("width", 0)), float(element.get("height", 0))
            boxes.append(Box(x, y, x + w, y + h))
        elif tag == "ellipse" and "dag-node" in classes:
            cx, cy = float(element.get("cx", 0)), float(element.get("cy", 0))
            rx, ry = float(element.get("rx", 0)), float(element.get("ry", 0))
            boxes.append(Box(cx - rx, cy - ry, cx + rx, cy + ry))
        elif tag == "path" and "dag-edge" in classes:
            edges.append(_path_points(element.get("d", "")))
        elif tag == "line" and "dag-edge" in classes:
            edges.append(
                (
                    (float(element.get("x1", 0)), float(element.get("y1", 0))),
                    (float(element.get("x2", 0)), float(element.get("y2", 0))),
                )
            )
        elif tag == "text" and "font-size" in element.attrib:
            sizes.append(float(element.get("font-size", 0)))
    return Drawing(view_box[2], view_box[3], tuple(boxes), tuple(edges), tuple(sizes))


def _consumer_graph() -> str:
    """12 nodes and 17 edges, the shape of a domain-name checker's diagram."""
    params = (("as_of", "2026-11-22"),)

    def step(name: str, title: str, description: str) -> NodeMetadata:
        return NodeMetadata(
            name=name, kind="step", title=title, description=description, params=params
        )

    def param(name: str) -> NodeMetadata:
        return NodeMetadata(name=name, kind="param", title=name)

    nodes = [
        param("name"),
        param("tlds"),
        param("variants"),
        param("with_lookalikes"),
        step("norm", "Normalize", "Lowercase the name, strip spaces and reject bad characters"),
        step("cand", "Candidates", "Build the full domain names from the name, TLDs and variants"),
        step("look", "Lookalikes", "Add names that look like the wanted one"),
        step("rdap", "RDAP lookup", "Ask each registry whether the domain is registered"),
        step("dns", "DNS probe", "Resolve each candidate and record NXDOMAIN answers"),
        step("price", "Pricing", "Fetch the registration price of each free candidate"),
        step("ctrl", "Controls", "Check trademark and blocklist rules for the name"),
        step("decide", "Decide", "Combine lookups, prices and controls into one verdict"),
    ]
    edges = [
        ("name", "norm"),
        ("norm", "cand"),
        ("tlds", "cand"),
        ("variants", "cand"),
        ("cand", "look"),
        ("with_lookalikes", "look"),
        ("look", "rdap"),
        ("look", "dns"),
        ("tlds", "price"),
        ("rdap", "price"),
        ("norm", "ctrl"),
        ("rdap", "decide"),
        ("dns", "decide"),
        ("price", "decide"),
        ("ctrl", "decide"),
        ("with_lookalikes", "decide"),
    ]
    return render_graph_svg(nodes, edges, output_label="Verdict", final_step_name="decide")


def _drawings() -> dict[str, str]:
    return {
        "consumer": _consumer_graph(),
        "docs_graph": (DOCS_ASSETS / "graph.svg").read_text(),
        "docs_graph_states": (DOCS_ASSETS / "graph_states.svg").read_text(),
    }


@pytest.fixture(scope="module")
def drawings() -> dict[str, Drawing]:
    return {name: read_drawing(svg) for name, svg in _drawings().items()}


GRAPHS = ["consumer", "docs_graph", "docs_graph_states"]


@pytest.mark.parametrize("graph", GRAPHS)
def test_no_two_boxes_overlap(graph: str, drawings: dict[str, Drawing]) -> None:
    boxes = drawings[graph].boxes
    assert boxes
    for i, first in enumerate(boxes):
        for second in boxes[i + 1 :]:
            assert not first.overlaps(second), (first, second)


@pytest.mark.parametrize("graph", GRAPHS)
def test_every_box_is_inside_the_view_box(graph: str, drawings: dict[str, Drawing]) -> None:
    drawing = drawings[graph]
    for box in drawing.boxes:
        assert box.left >= 0 and box.top >= 0, box
        assert box.right <= drawing.width and box.bottom <= drawing.height, box


@pytest.mark.xfail(strict=True, reason="titles are 14 px, descriptions 10 px, params 9 px")
@pytest.mark.parametrize("graph", GRAPHS)
def test_every_text_is_readable(graph: str, drawings: dict[str, Drawing]) -> None:
    sizes = drawings[graph].font_sizes
    assert sizes
    assert min(sizes) >= MIN_FONT_SIZE


def _selectors(stylesheet: str) -> set[str]:
    return {s.strip() for s in re.findall(r"([^{}]+)\{", stylesheet)}


def test_light_and_dark_stylesheets_style_the_same_selectors() -> None:
    assert _selectors(_STYLESHEET_LIGHT) == _selectors(_STYLESHEET_DARK)


def _edge_runs_under_a_box(drawing: Drawing) -> bool:
    """True when an edge point sits inside a box that is not one of its two ends."""
    arrow = 8
    for points in drawing.edges:
        start, end = points[0], points[-1]

        def is_end(
            box: Box, start: tuple[float, float] = start, end: tuple[float, float] = end
        ) -> bool:
            leaves = box.left - 1 <= start[0] <= box.right + 1 and abs(box.bottom - start[1]) < 2
            arrives = box.left - 1 <= end[0] <= box.right + 1 and abs(box.top - end[1] - arrow) < 2
            return leaves or arrives

        for box in drawing.boxes:
            if is_end(box):
                continue
            if any(box.contains(x, y, margin=1.0) for x, y in points):
                return True
    return False


@pytest.mark.parametrize(
    "graph",
    [
        pytest.param(
            "consumer",
            marks=pytest.mark.xfail(
                strict=True, reason="edges are routed without lanes and pass under boxes"
            ),
        ),
        "docs_graph",
        "docs_graph_states",
    ],
)
def test_no_edge_runs_under_a_box(graph: str, drawings: dict[str, Drawing]) -> None:
    assert not _edge_runs_under_a_box(drawings[graph])
