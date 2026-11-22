"""Golden images: each fixture graph, drawn, must match the committed SVG text.

A change to the look shows up as a failing test with the first differing line. To accept it:

    ADAG_UPDATE_GOLDEN=1 pytest tests/test_golden_graphs.py

That rewrites ``tests/golden_graphs/*.svg`` and, when a Chrome or Chromium binary is on the path,
a same-name ``.png`` to look at. The PNGs are for review only: no test reads them. Look at the
changed PNGs before you commit them.

Coordinates carry one decimal and the layout has no random or set-order step, so the same graph
always draws the same text. To cover a new drawing bug, add the graph to ``tests/graphs_fixtures.py``
and a line to ``SPECS`` here.
"""

import itertools
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from adag.visualization import SvgOptions, render_graph_svg
from graphs_fixtures import GRAPHS

GOLDEN = Path(__file__).resolve().parent / "golden_graphs"
UPDATE = os.environ.get("ADAG_UPDATE_GOLDEN") == "1"
COMMAND = "ADAG_UPDATE_GOLDEN=1 pytest tests/test_golden_graphs.py"


@dataclass(frozen=True)
class Spec:
    """One golden image: a graph in one style, input mode, density and theme."""

    graph: str
    style: str = "blueprint"
    inputs: str = "terminals"
    density: str = "normal"
    theme: str = "light"

    @property
    def stem(self) -> str:
        return f"{self.graph}__{self.style}-{self.inputs}-{self.density}-{self.theme}"


def _specs() -> list[Spec]:
    specs: list[Spec] = []
    for graph in ("chain3", "diamond", "fan_out_input", "mapped", "loop", "blocked", "long_edge"):
        specs += [Spec(graph, theme="light"), Spec(graph, theme="dark")]
    for style, inputs, density in itertools.product(
        ("blueprint", "band"), ("terminals", "tabs", "rows"), ("normal", "compact")
    ):
        specs.append(Spec("diamond_inputs", style, inputs, density))
    specs += [Spec("diamond_inputs", "classic", "terminals", d) for d in ("normal", "compact")]
    for graph in ("namecheck", "namecheck_big"):
        specs += [
            Spec(graph),
            Spec(graph, "band", "rows", "compact"),
            Spec(graph, theme="dark"),
        ]
    return specs


SPECS = _specs()


def render(spec: Spec) -> str:
    """The SVG of ``spec``, one element per line so a difference is easy to find."""
    graph = GRAPHS[spec.graph]
    svg = render_graph_svg(
        list(graph.nodes),
        list(graph.edges),
        output_label=graph.output_label,
        final_step_name=graph.final,
        options=SvgOptions(
            theme=spec.theme, style=spec.style, inputs=spec.inputs, density=spec.density
        ),
    )
    return svg.replace("><", ">\n<") + "\n"


def _write_png(svg_path: Path) -> None:
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if chrome is None:
        return
    size = re.search(r'width="([\d.]+)" height="([\d.]+)"', svg_path.read_text()[:2000])
    if size is None:
        return
    width, height = int(float(size.group(1))), int(float(size.group(2)))
    command = [
        chrome,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        f"--window-size={width},{height}",
        f"--screenshot={svg_path.with_suffix('.png')}",
        f"file://{svg_path}",
    ]
    nice = shutil.which("nice")
    if nice is not None:
        command = [nice, "-n", "19", *command]
    subprocess.run(command, check=False, capture_output=True, timeout=60)


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.stem)
def test_the_graph_matches_its_golden_svg(spec: Spec) -> None:
    path = GOLDEN / f"{spec.stem}.svg"
    drawn = render(spec)
    if UPDATE:
        GOLDEN.mkdir(exist_ok=True)
        if not path.exists() or path.read_text() != drawn:
            path.write_text(drawn)
            _write_png(path)
        return
    assert path.exists(), f"{path.name} is missing. Create it with: {COMMAND}"
    expected = path.read_text()
    if drawn != expected:
        for number, (got, want) in enumerate(
            itertools.zip_longest(drawn.splitlines(), expected.splitlines(), fillvalue="<end>"),
            start=1,
        ):
            if got != want:
                pytest.fail(
                    f"{path.name} differs at line {number}.\n  now:    {got[:200]}\n"
                    f"  golden: {want[:200]}\nIf the change is right, run: {COMMAND}"
                )


def test_every_golden_file_has_a_spec_and_the_other_way_round() -> None:
    wanted = {f"{spec.stem}.svg" for spec in SPECS}
    present: set[str] = {p.name for p in GOLDEN.glob("*.svg")} if GOLDEN.exists() else set()
    if UPDATE:
        for stale in present - wanted:
            (GOLDEN / stale).unlink()
            (GOLDEN / stale).with_suffix(".png").unlink(missing_ok=True)
        return
    assert present == wanted, (
        f"stale: {sorted(present - wanted)}, missing: {sorted(wanted - present)}"
    )
