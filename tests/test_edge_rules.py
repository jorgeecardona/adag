"""Edges branch and never merge; edges of different sources keep a lane apart (band: no hooks).

``tests/edge_rules.py`` reads the drawn SVG and lists every broken rule. Each graph in
``tests/graphs_fixtures.py`` (and the frame graphs) is drawn in blueprint and band, with every input
mode and density.
"""

import itertools
import math
import re
import xml.etree.ElementTree as ET

import pytest

from adag.svg_paint import Density, Inputs
from adag.visualization import SvgOptions, render_graph_svg
from edge_rules import NS, violations
from graphs_fixtures import FRAME_GRAPHS, GRAPHS, GraphFixture

ALL = {**GRAPHS, **FRAME_GRAPHS}
COMBOS: list[tuple[str, Inputs, Density]] = [
    (style, inputs, density)
    for style in ("blueprint", "band")
    for inputs, density in itertools.product(("terminals", "tabs", "rows"), ("normal", "compact"))
]
CASES = [(name, *combo) for name in sorted(ALL) for combo in COMBOS]
# Cases where the rules are not met yet, with the most violations allowed. Fewer bends come first:
# when a lane of 12 px would cost an extra bend, the edge keeps its place and the case is listed
# here. The 41-node stress graph (``layered_40``) has 70 edges over 10 levels.
# Lower a number when a fix removes a violation. The branch dots of blueprint raised two numbers on
# 2026-11-22, because they show short shared stretches that round corners hid. See docs/issues.
KNOWN: dict[tuple[str, str, str, str], int] = {
    ("etl_fan_in", "blueprint", "tabs", "compact"): 1,
    ("etl_fan_in", "blueprint", "rows", "compact"): 1,
    ("etl_fan_in", "band", "terminals", "compact"): 1,
    ("namecheck", "blueprint", "terminals", "compact"): 2,
    ("namecheck_stateless", "blueprint", "terminals", "compact"): 2,
    ("namecheck_big", "blueprint", "terminals", "normal"): 2,
    ("namecheck_big", "blueprint", "terminals", "compact"): 1,
    ("namecheck_big", "band", "terminals", "normal"): 2,
    **{("layered_40", "blueprint", i, "normal"): 2 for i in ("terminals", "tabs", "rows")},
    **{("layered_40", "blueprint", i, "compact"): 8 for i in ("terminals", "tabs", "rows")},
    # A join and a leave 12 px apart on one trunk. Round corners hid it; the branch dots show it.
    ("ml_training", "blueprint", "terminals", "normal"): 1,
    **{
        ("layered_40", "band", i, d): 3
        for i in ("terminals", "tabs", "rows")
        for d in ("normal", "compact")
    },
}
MIN_RADIUS = 24.0
SHORT_PATH = 48.0


def render(graph: GraphFixture, style: str, inputs: Inputs, density: Density) -> str:
    return render_graph_svg(
        list(graph.nodes),
        list(graph.edges),
        output_label=graph.output_label,
        final_step_name=graph.final,
        options=SvgOptions(style=style, inputs=inputs, density=density),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(("name", "style", "inputs", "density"), CASES)
def test_edges_branch_and_never_merge(
    name: str, style: str, inputs: Inputs, density: Density
) -> None:
    found = violations(render(ALL[name], style, inputs, density))
    allowed = KNOWN.get((name, style, inputs, density), 0)
    assert len(found) <= allowed, "\n".join(found)


def _band_corner_radii(d: str) -> tuple[list[float], float]:
    """The radius of every ``Q`` corner of a path, and the length of the whole path."""
    tokens = re.findall(r"[MLCQ]|-?\d+(?:\.\d+)?", d)
    radii: list[float] = []
    length = 0.0
    x = y = 0.0
    i = 0
    while i < len(tokens):
        command = tokens[i]
        i += 1
        count = {"M": 1, "L": 1, "C": 3, "Q": 2}[command]
        pts = [(float(tokens[i + 2 * k]), float(tokens[i + 2 * k + 1])) for k in range(count)]
        i += 2 * count
        if command == "Q":
            radii.append(math.hypot(pts[0][0] - x, pts[0][1] - y))
        if command != "M":
            length += math.hypot(pts[-1][0] - x, pts[-1][1] - y)
        x, y = pts[-1]
    return radii, length


@pytest.mark.parametrize(
    ("name", "inputs", "density"),
    [
        (n, i, d)
        for n in sorted(ALL)
        for i, d in itertools.product(("terminals", "tabs", "rows"), ("normal", "compact"))
    ],
)
def test_a_band_edge_has_no_hook(name: str, inputs: Inputs, density: Density) -> None:
    root = ET.fromstring(render(ALL[name], "band", inputs, density))  # noqa: S314
    for path in root.iter(f"{NS}path"):
        if "dag-edge" not in path.get("class", "").split() or path.get("data-source") is None:
            continue
        radii, length = _band_corner_radii(path.get("d", ""))
        if length >= SHORT_PATH:
            assert all(r >= MIN_RADIUS - 0.5 for r in radii), (
                path.get("data-source"),
                path.get("data-target"),
                radii,
            )


@pytest.mark.parametrize("name", sorted(ALL))
def test_a_band_edge_ends_on_the_top_of_its_target(name: str) -> None:
    root = ET.fromstring(render(ALL[name], "band", "terminals", "normal"))  # noqa: S314
    for polygon in root.iter(f"{NS}polygon"):
        if "dag-arrow--feedback" in polygon.get("class", "").split():
            continue
        points = [tuple(float(v) for v in p.split(",")) for p in polygon.get("points", "").split()]
        base_y = {round(p[1], 1) for p in points[:2]}
        assert len(base_y) == 1, points  # the base of the arrowhead is level: it points down
