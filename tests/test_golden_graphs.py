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
from pydantic import BaseModel

from adag import InputParam, NullObserver, Pipeline, Step, dag
from adag.svg_paint import Corners, Density, Inputs, Style
from adag.svg_theme import stylesheet
from adag.visualization import SvgOptions, render_graph_svg
from graphs_fixtures import GRAPHS

GOLDEN = Path(__file__).resolve().parent / "golden_graphs"
UPDATE = os.environ.get("ADAG_UPDATE_GOLDEN") == "1"
COMMAND = "ADAG_UPDATE_GOLDEN=1 pytest tests/test_golden_graphs.py"


@dataclass(frozen=True)
class Spec:
    """One golden image: a graph in one style, input mode, density and theme."""

    graph: str
    style: Style = "blueprint"
    inputs: Inputs = "terminals"
    density: Density = "normal"
    theme: str = "light"
    legend: bool = False
    animate: bool = False
    corners: Corners = "round"

    @property
    def stem(self) -> str:
        extras = (
            ("-legend" if self.legend else "")
            + ("-animate" if self.animate else "")
            + ("-sharp" if self.corners == "sharp" else "")
        )
        return f"{self.graph}__{self.style}-{self.inputs}-{self.density}-{self.theme}{extras}"


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
    specs += [
        Spec("blocked", "classic"),
        Spec("dataset_middle", "band", "tabs"),
        Spec("diamond_stateless", "band", "rows"),
        Spec("namecheck_stateless", "classic"),
        Spec("diamond", legend=True, animate=True),
        Spec("namecheck", corners="sharp"),
        Spec("diamond_inputs", "band", "tabs", corners="sharp"),
    ]
    return specs


SPECS = _specs()
REAL = ("dag_real__stateless", "dag_real__cached")


class Num(BaseModel):
    value: int


@dataclass(frozen=True)
class Source(Step[Num]):
    """Read a number."""

    def run(self, x: int) -> Num:
        return Num(value=x)


@dataclass(frozen=True)
class Scale(Step[Num]):
    """Multiply the number."""

    factor: int = 2

    def run(self, source: Num, factor: int) -> Num:
        return Num(value=source.value * factor)


def _real_graph(factor: int):
    """Three steps from real classes. Two are ``Scale``, so the second is named ``Scale#2``.

    The cache key of a step covers its code, so editing a step in this file changes the keys in the
    golden images: run the update command.
    """
    x = InputParam.with_default("x", 3)
    return Scale(factor=factor)(source=Scale()(source=Source()(x=x)))


def render(spec: Spec) -> str:
    """The SVG of ``spec``, one element per line so a difference is easy to find."""
    graph = GRAPHS[spec.graph]
    svg = render_graph_svg(
        list(graph.nodes),
        list(graph.edges),
        output_label=graph.output_label,
        final_step_name=graph.final,
        options=SvgOptions(
            theme=spec.theme,
            style=spec.style,
            inputs=spec.inputs,
            density=spec.density,
            legend=spec.legend,
            animate=spec.animate,
            corners=spec.corners,
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


def _check(stem: str, drawn: str) -> None:
    """Compare ``drawn`` with ``golden_graphs/<stem>.svg``, or write it when updating."""
    path = GOLDEN / f"{stem}.svg"
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


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.stem)
def test_the_graph_matches_its_golden_svg(spec: Spec) -> None:
    _check(spec.stem, render(spec))


def _pretty(svg: str) -> str:
    return svg.replace("><", ">\n<") + "\n"


def test_a_real_dag_with_no_cache_matches_its_golden_svg() -> None:
    """Real step classes: a ``#2`` name, real cache keys, neutral because there is no cache_dir."""
    _check(REAL[0], _pretty(str(dag(_real_graph(2), options=SvgOptions(inputs="rows")))))


def test_a_real_dag_with_a_cache_matches_its_golden_svg(tmp_path: Path) -> None:
    """The first two steps are cached; the last changed its field, so it is pending."""
    Pipeline(_real_graph(2), cache_dir=tmp_path).execute(observer=NullObserver())
    drawn = str(dag(_real_graph(5), cache_dir=tmp_path, options=SvgOptions(style="band")))
    _check(REAL[1], _pretty(drawn))


def test_every_golden_file_has_a_spec_and_the_other_way_round() -> None:
    wanted = {f"{spec.stem}.svg" for spec in SPECS} | {f"{stem}.svg" for stem in REAL}
    present: set[str] = {p.name for p in GOLDEN.glob("*.svg")} if GOLDEN.exists() else set()
    if UPDATE:
        for stale in present - wanted:
            (GOLDEN / stale).unlink()
            (GOLDEN / stale).with_suffix(".png").unlink(missing_ok=True)
        return
    assert present == wanted, (
        f"stale: {sorted(present - wanted)}, missing: {sorted(wanted - present)}"
    )


def test_every_class_the_stylesheet_names_is_used_by_some_golden_image() -> None:
    """A class that no golden uses is dead CSS, or a look that no test draws."""
    named = set(re.findall(r"\.(dag[a-z0-9_-]*|is-[a-z]+)", stylesheet("light", animate=True)))
    used: set[str] = set()
    for path in GOLDEN.glob("*.svg"):
        drawing = re.sub(r"<style>.*?</style>", "", path.read_text(), flags=re.DOTALL)
        used.update(re.findall(r'class="([^"]*)"', drawing))
    used_tokens = {token for value in used for token in value.split()}
    assert named - used_tokens == set()
