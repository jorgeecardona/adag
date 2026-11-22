"""``style="classic"`` draws the boxes and the edges of adag 0.3.22 (commit 2811cba).

``golden_graphs/classic_0_3_22.json`` holds the boxes, ellipses, edge paths and arrowheads that
0.3.22 drew for some fixture graphs. The test draws the same graphs now and compares them. A box
or an ellipse may differ by 0.06 px (the new text carries one decimal). An edge path and an
arrowhead must be the same text. Colours, fonts, attributes and the stylesheet are not compared:
those are the fixes made since 0.3.22.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from adag.visualization import SvgOptions, render_graph_svg
from graphs_fixtures import GRAPHS

SVG_NS = "{http://www.w3.org/2000/svg}"
GOLDEN = json.loads(
    (Path(__file__).resolve().parent / "golden_graphs" / "classic_0_3_22.json").read_text()
)


def _numbers(values: list[str]) -> list[float]:
    return [float(n) for text in values for n in re.findall(r"-?\d+(?:\.\d+)?", text)]


def summarize(svg: str) -> dict[str, list[list[str]]]:
    """The boxes, ellipses, edges and arrowheads of a drawing, as lists of attribute text."""
    root = ET.fromstring(svg)  # noqa: S314
    found: dict[str, list[list[str]]] = {"rects": [], "ellipses": [], "edges": [], "arrows": []}
    for element in root.iter():
        tag = element.tag.replace(SVG_NS, "")
        a = element.attrib
        classes = a.get("class", "").split()
        if tag == "rect" and "dag-node" in classes:
            found["rects"].append([a["x"], a["y"], a["width"], a["height"], a["rx"], *classes])
        elif tag == "ellipse":
            found["ellipses"].append([a["cx"], a["cy"], a["rx"], a["ry"], *classes])
        elif tag in ("path", "line") and "dag-edge" in classes:
            d = a.get("d") or " ".join(a[k] for k in ("x1", "y1", "x2", "y2"))
            found["edges"].append([tag, d, *classes])
        elif tag == "polygon":
            found["arrows"].append([a["points"], *classes])
    return found


@pytest.mark.parametrize("graph", sorted(GOLDEN))
def test_classic_draws_the_boxes_and_edges_of_0_3_22(graph: str) -> None:
    fixture = GRAPHS[graph]
    svg = render_graph_svg(
        list(fixture.nodes),
        list(fixture.edges),
        output_label=fixture.output_label,
        final_step_name=fixture.final,
        options=SvgOptions(style="classic"),
    )
    now, then = summarize(svg), GOLDEN[graph]
    root = ET.fromstring(svg)  # noqa: S314
    assert [float(root.get("width", "0")), float(root.get("height", "0"))] == pytest.approx(
        [float(v) for v in then["size"]], abs=0.1
    )
    for key in ("rects", "ellipses"):
        assert len(now[key]) == len(then[key]), key
        count = 5 if key == "rects" else 4
        for new_item, old_item in zip(now[key], then[key], strict=True):
            assert _numbers(new_item[:count]) == pytest.approx(_numbers(old_item[:count]), abs=0.06)
            assert new_item[count:] == old_item[-1].split()
    assert now["edges"] == [[e[0], e[1], *e[2].split()] for e in then["edges"]]
    assert now["arrows"] == [[a[0], *a[1].split()] for a in then["arrows"]]


def test_classic_keeps_its_slow_layout_within_a_generous_bound() -> None:
    fixture = GRAPHS["namecheck"]
    start = time.perf_counter()
    render_graph_svg(
        list(fixture.nodes),
        list(fixture.edges),
        output_label=fixture.output_label,
        final_step_name=fixture.final,
        options=SvgOptions(style="classic"),
    )
    assert time.perf_counter() - start < 20.0
