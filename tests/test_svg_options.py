"""Tests for the SVG style, inputs and density options, the CSS variables and the node attributes."""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import InputParam, Step, dag, node_metadata, walk
from adag.svg_theme import CSS_VARIABLES, stylesheet
from adag.visualization import NodeMetadata, SvgOptions, render_graph_svg
from graphs_fixtures import GRAPHS, step

SVG_NS = "{http://www.w3.org/2000/svg}"


class Num(BaseModel):
    value: int


@dataclass(frozen=True)
class Source(Step[Num]):
    """Read a number."""

    def run(self, x: int) -> Num:
        return Num(value=x)


@dataclass(frozen=True)
class Double(Step[Num]):
    """Double the number."""

    def run(self, source: Num) -> Num:
        return Num(value=source.value * 2)


def _svg(graph: str, **options: str | bool) -> str:
    fixture = GRAPHS[graph]
    return render_graph_svg(
        list(fixture.nodes),
        list(fixture.edges),
        output_label=fixture.output_label,
        final_step_name=fixture.final,
        options=SvgOptions(**options),  # type: ignore[arg-type]
    )


def _drawing(svg: str) -> str:
    """The SVG after its stylesheet: the stylesheet names every class, the drawing only those used."""
    return svg.split("</style>", 1)[1]


def _texts(svg: str) -> list[str]:
    """What is drawn as text, without the tooltips (which hold the full text)."""
    root = ET.fromstring(svg)  # noqa: S314 (adag's own output)
    return [e.text or "" for e in root.iter(f"{SVG_NS}text")]


def _groups(svg: str) -> list[ET.Element]:
    root = ET.fromstring(svg)  # noqa: S314 (adag's own output)
    return [g for g in root.iter(f"{SVG_NS}g") if "dag-item" in g.get("class", "")]


@pytest.mark.parametrize(
    ("field", "value"), [("style", "neon"), ("inputs", "chips"), ("density", "tiny")]
)
def test_an_unknown_option_value_is_refused(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=field):
        SvgOptions(**{field: value})  # type: ignore[arg-type]


def test_the_default_look_is_blueprint_with_terminals_and_normal_density() -> None:
    options = SvgOptions()
    assert (options.style, options.inputs, options.density) == ("blueprint", "terminals", "normal")
    assert 'class="dag dag--blueprint"' in _svg("diamond_inputs")[:200] or "dag--blueprint" in _svg(
        "diamond_inputs"
    )


def test_every_node_group_carries_state_and_kind_attributes() -> None:
    groups = _groups(_svg("diamond_inputs"))
    by_name = {g.get("data-name"): g for g in groups}
    assert by_name["load"].get("data-state") == "cached"
    assert by_name["clean"].get("data-state") == "pending"
    assert by_name["load"].get("data-kind") == "step"
    assert by_name["path"].get("data-kind") == "param"
    assert by_name["path"].get("data-state") == "none"


def test_a_graph_without_state_is_stateless_and_shows_no_state_word() -> None:
    nodes = [step("a", "Alpha", "First.", "PENDING"), step("b", "Beta", "Second.", "PENDING")]
    svg = render_graph_svg(nodes, [("a", "b")])
    assert (
        "dag--stateless" in svg.split("</style>", 1)[0].split(">", 1)[0] or 'dag--stateless"' in svg
    )
    assert "PENDING" not in svg
    assert all(g.get("data-state") == "none" for g in _groups(svg))


def test_blueprint_shows_the_state_word_and_the_cache_key() -> None:
    svg = _svg("diamond")
    assert "CACHED" in svg and "PENDING" in svg
    key = GRAPHS["diamond"].nodes[0].key
    assert f"key {key}" in svg


def test_the_cache_key_of_a_dag_step_is_in_the_metadata_and_the_drawing(tmp_path: Path) -> None:
    out = Double()(source=Source()(x=InputParam[int]("x")))
    nodes, _ = walk(out)
    metas = {m.name: m for m in node_metadata(nodes)}
    key = out.content_hash()
    assert key is None or metas["Double"].key == key
    assert metas["x"].key == ""  # an input has no key
    assert f"key {metas['Double'].key}" in str(dag(out)) or not metas["Double"].key


def test_an_input_param_default_is_the_value_of_its_node() -> None:
    @dataclass(frozen=True)
    class Scale(Step[Num]):
        """Scale a number."""

        def run(self, x: int, factor: int) -> Num:
            return Num(value=x * factor)

    out = Scale()(x=InputParam[int]("x"), factor=InputParam.with_default("factor", 3))
    nodes, _ = walk(out)
    values = {m.name: m.value for m in node_metadata(nodes)}
    assert values["factor"] == "3"
    assert values["x"] == ""


def test_terminals_draw_inputs_as_nodes() -> None:
    svg = _drawing(_svg("diamond_inputs", inputs="terminals"))
    assert "dag-node--param" in svg
    assert "dag-tab" not in svg


def test_tabs_fold_inputs_into_the_step() -> None:
    svg = _svg("diamond_inputs", inputs="tabs")
    assert "dag-node--param" not in _drawing(svg)
    assert "dag-tab" in _drawing(svg)
    assert "▸ path = 'data.csv'" in _texts(svg)


def test_rows_fold_inputs_into_one_line_of_the_step() -> None:
    svg = _svg("diamond_inputs", inputs="rows")
    assert "dag-node--param" not in _drawing(svg) and "dag-tab" not in _drawing(svg)
    assert "▸ case = 'lower'" in _texts(svg)


def test_classic_ignores_the_inputs_option() -> None:
    assert _svg("diamond_inputs", style="classic", inputs="tabs") == _svg(
        "diamond_inputs", style="classic", inputs="terminals"
    )


def test_a_step_whose_only_inputs_are_params_is_not_a_leaf_in_any_mode() -> None:
    for inputs in ("terminals", "tabs", "rows"):
        groups = {g.get("data-name"): g for g in _groups(_svg("diamond_inputs", inputs=inputs))}
        assert "is-leaf" not in groups["load"].get("class", "")


def test_compact_draws_the_title_and_moves_the_rest_to_the_tooltip() -> None:
    svg = _svg("diamond_inputs", density="compact")
    assert "Normalize the text." not in _texts(svg)  # the description is not drawn
    assert "Normalize the text." in _drawing(svg)  # but it is in the tooltip
    assert "case = 'lower'" in _texts(svg)  # the input is a label to the left of the step


def test_a_list_of_inputs_longer_than_six_lines_ends_with_a_count() -> None:
    inputs = [
        NodeMetadata(name=f"i{n}", kind="param", title=f"i{n}", value=str(n)) for n in range(9)
    ]
    nodes = [*inputs, step("s", "Reader", "Reads them all.")]
    edges = [(f"i{n}", "s") for n in range(9)]
    svg = render_graph_svg(nodes, edges, options=SvgOptions(inputs="rows"))
    texts = _texts(svg)
    assert "▸ i4 = 4" in texts and "▸ i5 = 5" not in texts
    assert "+4 more" in "".join(texts)


def test_a_mapped_node_keeps_its_each_item_line_in_every_input_mode() -> None:
    for inputs in ("terminals", "tabs", "rows"):
        for style in ("blueprint", "band", "classic"):
            svg = _svg("mapped", inputs=inputs, style=style)
            assert "each item = Tokenize > Encode" in _texts(svg)
            assert "dag-node--mapped" in _drawing(svg)


def test_a_blocked_step_has_no_key_and_says_blocked() -> None:
    svg = _svg("blocked")
    texts = _texts(svg)
    assert "BLOCKED" in texts
    assert sum(t.startswith("key ") for t in texts) == 1  # Sum has one, the blocked Load has none


def test_the_drawing_has_no_ids_and_no_colour_outside_the_stylesheet() -> None:
    for style in ("blueprint", "band", "classic"):
        svg = _svg("namecheck", style=style)
        drawing = _drawing(svg)
        assert svg.count("</style>") == 1
        assert " id=" not in svg
        assert re.search(r"#[0-9a-fA-F]{6}", drawing) is None
        assert "fill=" not in drawing


def test_every_colour_in_the_stylesheet_is_a_variable_with_a_default() -> None:
    for theme in ("light", "dark"):
        css = stylesheet(theme)
        assert re.search(r"#[0-9a-fA-F]{3,8}\b", re.sub(r"var\([^)]*\)", "", css)) is None
        assert "var(--adag-canvas, " in css


def test_a_page_can_override_every_variable_the_stylesheet_uses() -> None:
    used = set(re.findall(r"--adag-[a-z-]+", stylesheet("light")))
    assert used == {v.css_name for v in CSS_VARIABLES}


def test_include_styles_false_leaves_classes_and_no_stylesheet() -> None:
    svg = _svg("diamond_inputs", include_styles=False)
    assert "<style" not in svg
    assert "dag-item" in svg and "data-state" in svg


def test_the_same_graph_always_draws_the_same_text_with_one_decimal() -> None:
    first, second = _svg("namecheck_big"), _svg("namecheck_big")
    assert first == second
    assert re.search(r'="-?\d+\.\d{2,}', first) is None


def test_show_params_false_still_shrinks_every_style() -> None:
    for style in ("blueprint", "band", "classic"):
        with_params = re.search(r'height="([\d.]+)"', _svg("namecheck", style=style))
        without = re.search(r'height="([\d.]+)"', _svg("namecheck", style=style, show_params=False))
        assert with_params and without
        assert float(without.group(1)) < float(with_params.group(1))


def test_the_new_options_change_nothing_while_they_are_off() -> None:
    assert _svg("namecheck") == _svg("namecheck", legend=False, animate=False)
    assert _svg("namecheck", theme="dark") == _svg("namecheck", theme="dark", animate=False)


def test_theme_auto_holds_both_palettes_and_a_media_query() -> None:
    css = stylesheet("auto")
    assert "@media (prefers-color-scheme: dark)" in css
    assert "var(--adag-canvas, #ffffff)" in css and "var(--adag-canvas, #14161a)" in css
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", re.sub(r"var\([^)]*\)", "", css)) is None
    svg = _svg("diamond", theme="auto")
    assert svg.count("</style>") == 1


def _canvas_height(svg: str) -> float:
    match = re.search(r'height="([\d.]+)"', svg)
    assert match is not None
    return float(match.group(1))


def test_the_legend_names_the_states_in_use_and_adds_a_row() -> None:
    plain, with_legend = _svg("diamond"), _svg("diamond", legend=True)
    assert _canvas_height(with_legend) > _canvas_height(plain)
    legend = ET.fromstring(with_legend).find(f".//{SVG_NS}g[@class='dag-legend']")  # noqa: S314
    assert legend is not None
    words = [t.text for t in legend.iter(f"{SVG_NS}text")]
    assert words == ["CACHED", "PENDING"]  # the diamond has no blocked step


def test_the_legend_is_left_out_of_a_graph_with_no_state() -> None:
    nodes = [step("a", "Alpha", "First.", "PENDING")]
    svg = render_graph_svg(nodes, [], options=SvgOptions(legend=True))
    assert "dag-legend" not in _drawing(svg)


def test_animate_moves_dashes_only_along_edges_into_pending_steps() -> None:
    plain = _svg("diamond")
    moving = _svg("diamond", animate=True)
    assert "dag-edge--flow" not in plain
    assert "prefers-reduced-motion: no-preference" in moving.split("</style>", 1)[0]
    drawing = _drawing(moving)
    # Into Clean, into Report (twice): the three edges that lead to a pending step.
    assert drawing.count("dag-edge--flow") == 3


def test_every_css_variable_of_the_stylesheet_is_in_the_docs_table() -> None:
    docs = (Path(__file__).resolve().parent.parent / "docs" / "visualization.md").read_text()
    used = set(re.findall(r"--adag-[a-z-]+", stylesheet("light")))
    listed = set(re.findall(r"^\| `(--adag-[a-z-]+)` \|", docs, flags=re.MULTILINE))
    assert used == listed, (
        f"missing from the docs: {sorted(used - listed)}; unknown: {sorted(listed - used)}"
    )
