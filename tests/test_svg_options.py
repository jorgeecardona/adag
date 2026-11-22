"""Tests for the SVG style, inputs and density options, the CSS variables and the node attributes."""

import re
import shutil
import subprocess
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import InputParam, Step, dag, node_metadata, walk
from adag.svg_theme import CSS_VARIABLES, stylesheet
from adag.visualization import NodeMetadata, SvgOptions, render_graph_svg
from graphs_fixtures import GRAPHS, file_input, param, step

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
    root = ET.fromstring(svg)  # noqa: S314
    return [e.text or "" for e in root.iter(f"{SVG_NS}text")]


def _groups(svg: str) -> list[ET.Element]:
    root = ET.fromstring(svg)  # noqa: S314
    return [g for g in root.iter(f"{SVG_NS}g") if "dag-item" in g.get("class", "")]


@pytest.mark.parametrize(
    ("field", "value"),
    [("style", "neon"), ("inputs", "chips"), ("density", "tiny"), ("corners", "bevel")],
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
    assert 'class="dag dag--blueprint dag--stateless"' in svg
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


def test_compact_with_terminals_draws_small_input_nodes_in_the_top_row() -> None:
    svg = _svg("diamond_inputs", density="compact")
    assert "dag-node--param" in _drawing(svg)
    assert "case = 'lower'" not in _texts(svg)  # no label to the left of a step
    assert "'lower'" in _texts(svg)  # the value sits under the name on the terminal


@pytest.mark.parametrize("inputs", ["tabs", "rows"])
def test_compact_with_tabs_or_rows_keeps_the_labels_left_of_the_step(inputs: str) -> None:
    svg = _svg("diamond_inputs", density="compact", inputs=inputs)
    assert "dag-node--param" not in _drawing(svg)
    assert "case = 'lower'" in _texts(svg)


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


def _split_supports(css: str) -> tuple[str, str]:
    """The literal part of a sheet, and the part inside ``@supports``."""
    index = css.index("@supports")
    return css[:index], css[index:]


def test_the_part_of_the_stylesheet_outside_supports_has_no_var() -> None:
    for theme in ("light", "dark", "auto"):
        literal, variables = _split_supports(stylesheet(theme))
        assert "var(" not in literal
        assert re.search(r"#[0-9a-fA-F]{3,8}\b", literal) is not None
        assert "var(--adag-canvas, " in variables


def test_every_colour_inside_supports_is_a_variable_with_a_default() -> None:
    for theme in ("light", "dark", "auto"):
        _, variables = _split_supports(stylesheet(theme))
        assert re.search(r"#[0-9a-fA-F]{3,8}\b", re.sub(r"var\([^)]*\)", "", variables)) is None


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


def test_theme_auto_holds_both_palettes_and_a_media_query() -> None:
    css = stylesheet("auto")
    assert css.count("@media (prefers-color-scheme: dark)") == 2  # literal and variables
    assert "var(--adag-canvas, #ffffff)" in css and "var(--adag-canvas, #14161a)" in css
    assert "fill: #14161a" in css  # the dark literal colours
    assert _svg("diamond", theme="auto").count("</style>") == 1


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


def test_classic_keeps_the_pending_colours_of_0_3_22_without_a_cache_dir() -> None:
    svg = _svg("diamond_stateless", style="classic")
    assert "dag--stateless" not in svg.split("</style>", 1)[1][:200]
    assert {g.get("data-state") for g in _groups(svg) if g.get("data-kind") == "step"} == {
        "pending"
    }


def test_a_file_input_in_classic_is_the_brown_rectangle_of_0_3_22() -> None:
    svg = _svg("blocked", style="classic")
    root = ET.fromstring(svg)  # noqa: S314
    rects = [r for r in root.iter(f"{SVG_NS}rect") if "dag-node--file" in r.get("class", "")]
    assert len(rects) == 1 and rects[0].get("rx") == "4"
    assert "sales.csv" in [t.get("class") and t.text for t in root.iter(f"{SVG_NS}text")]
    assert any("dag-text--file" in t.get("class", "") for t in root.iter(f"{SVG_NS}text"))


def test_the_first_release_helper_keeps_its_two_value_return() -> None:
    from adag.io import FileSource
    from adag.visualization import _node_name_and_kind  # pyright: ignore[reportPrivateUsage]

    assert _node_name_and_kind(InputParam[int]("x")) == ("x", "param")
    assert _node_name_and_kind(FileSource("data/a.csv")) == ("a.csv", "file")


def test_the_old_layout_node_name_still_imports() -> None:
    from adag.visualization import LayoutNode

    assert LayoutNode is not None


def test_the_helpers_imported_into_visualization_are_not_public() -> None:
    import adag.visualization as module

    for name in ("Look", "RNode", "draw", "STYLES", "INPUTS", "DENSITIES"):
        assert not hasattr(module, name)


def test_every_text_has_a_font_family_attribute_and_a_class_can_still_override_it() -> None:
    svg = _svg("namecheck")
    root = ET.fromstring(svg)  # noqa: S314
    texts = list(root.iter(f"{SVG_NS}text"))
    assert texts and all(t.get("font-family") for t in texts)
    assert "var(--adag-font, " in svg  # the stylesheet still sets the font by variable


def test_an_unknown_theme_warns_and_draws_light() -> None:
    with pytest.warns(UserWarning, match="theme"):
        options = SvgOptions(theme="sepia")
    assert options.stylesheet() == stylesheet("light")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for theme in ("light", "dark", "auto"):
            SvgOptions(theme=theme)


def test_an_input_that_nothing_reads_stays_drawn_when_inputs_are_folded() -> None:
    nodes = [param("unused", "1"), param("used"), step("a", "Alpha")]
    svg = render_graph_svg(nodes, [("used", "a")], options=SvgOptions(inputs="rows"))
    names = [g.get("data-name") for g in _groups(svg)]
    assert "unused" in names and "used" not in names


def test_a_graph_of_only_inputs_draws() -> None:
    svg = render_graph_svg([param("x", "1"), param("y")], [], options=SvgOptions(inputs="tabs"))
    assert [g.get("data-name") for g in _groups(svg)] == ["x", "y"]


def test_a_node_that_is_not_a_step_is_never_folded() -> None:
    for inputs in ("tabs", "rows"):
        svg = _svg("dataset_middle", inputs=inputs)
        by_name = {g.get("data-name"): g for g in _groups(svg)}
        assert "table" in by_name and by_name["table"].get("data-kind") == "dataset"
        assert "source" not in by_name  # the real input feeds a step and is folded


def test_a_duplicate_edge_is_drawn_once() -> None:
    nodes = [step("a", "A"), step("b", "B")]
    once = render_graph_svg(nodes, [("a", "b")])
    twice = render_graph_svg(nodes, [("a", "b"), ("a", "b")])
    assert once == twice


def test_a_narrow_graph_is_as_wide_as_its_legend() -> None:
    nodes = [step("a", "A", state="CACHED"), step("b", "B", state="PENDING")]
    with_legend = render_graph_svg(nodes, [], options=SvgOptions(legend=True, density="compact"))
    root = ET.fromstring(with_legend)  # noqa: S314
    width = float(root.get("width", "0"))
    last = max(
        float(t.get("x", 0)) + len(t.text or "") * 11 * 0.7
        for t in root.iter(f"{SVG_NS}text")
        if "dag-text--state" in t.get("class", "")
    )
    assert last <= width


def test_the_first_tab_fits_inside_its_box() -> None:
    nodes = [param("p" * 60, "v" * 60), step("a", "Alpha", "Short.")]
    svg = render_graph_svg(nodes, [("p" * 60, "a")], options=SvgOptions(inputs="tabs"))
    root = ET.fromstring(svg)  # noqa: S314
    tab = next(e for e in root.iter(f"{SVG_NS}path") if e.get("class") == "dag-tab")
    box = next(e for e in root.iter(f"{SVG_NS}rect") if "dag-node--step" in e.get("class", ""))
    tab_right = max(float(n) for n in re.findall(r"H(-?[\d.]+)", tab.get("d", "")))
    assert tab_right <= float(box.get("x", 0)) + float(box.get("width", 0))


def test_control_characters_are_dropped_from_text_and_tooltips() -> None:
    nodes = [step("a", "bad\x1bchar\x00", params=(("k", "a\x08b"),))]
    svg = render_graph_svg(nodes, [])
    assert not re.search("[\x00-\x08\x0b\x0c\x0e-\x1f]", svg)
    ET.fromstring(svg)  # noqa: S314


def test_a_file_shows_its_name_and_keeps_the_path_in_the_tooltip() -> None:
    nodes = [file_input("sales.csv", "data/2026/sales.csv"), step("a", "Alpha")]
    svg = render_graph_svg(nodes, [("sales.csv", "a")])
    assert "data/2026/sales.csv" not in _texts(svg)
    assert "data/2026/sales.csv" in _drawing(svg)  # in the tooltip


def test_a_long_param_default_is_cut_with_dots() -> None:
    long = InputParam.with_default("p", "x" * 100)
    out = Double()(source=Source()(x=long))
    nodes, _ = walk(out)
    value = {m.name: m.value for m in node_metadata(nodes)}["p"]
    assert len(value) == 40 and value.endswith("...")


def _magick_png(svg: Path, png: Path) -> bool:
    magick = shutil.which("magick")
    if magick is None:
        return False
    result = subprocess.run(
        [magick, "-background", "white", str(svg), str(png)],
        capture_output=True,
        check=False,
        timeout=60,
    )
    return result.returncode == 0 and png.exists()


@pytest.mark.parametrize("style", ["blueprint", "band", "classic"])
def test_a_renderer_without_css_variables_does_not_paint_the_graph_black(
    style: str, tmp_path: Path
) -> None:
    """ImageMagick ignores ``var()``: the literal colours must carry the drawing."""
    if shutil.which("magick") is None:
        pytest.skip("magick is not installed")
    svg_path, png_path = tmp_path / "graph.svg", tmp_path / "graph.png"
    svg_path.write_text(_svg("diamond_inputs", style=style))
    if not _magick_png(svg_path, png_path):
        pytest.skip("magick cannot read the SVG here")
    mean = subprocess.run(
        [
            shutil.which("magick") or "",
            str(png_path),
            "-colorspace",
            "Gray",
            "-format",
            "%[fx:mean]",
            "info:",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert float(mean) > 0.6  # a light drawing: mostly bright, not mostly black


def _radii(svg: str) -> set[float]:
    """The ``rx`` of every box in a drawing."""
    root = ET.fromstring(svg)  # noqa: S314
    return {float(r.get("rx", "0")) for r in root.iter(f"{SVG_NS}rect") if r.get("rx") is not None}


def _drawn(
    style: str, corners: str, graph: str = "diamond_inputs", inputs: str = "terminals"
) -> str:
    g = GRAPHS[graph]
    return render_graph_svg(
        list(g.nodes),
        list(g.edges),
        output_label=g.output_label,
        final_step_name=g.final,
        options=SvgOptions(style=style, corners=corners, inputs=inputs),  # type: ignore[arg-type]
    )


def test_the_default_corners_are_round() -> None:
    assert SvgOptions().corners == "round"
    assert _drawn("blueprint", "round") == render_graph_svg(
        list(GRAPHS["diamond_inputs"].nodes),
        list(GRAPHS["diamond_inputs"].edges),
        output_label=GRAPHS["diamond_inputs"].output_label,
        final_step_name=GRAPHS["diamond_inputs"].final,
    )


@pytest.mark.parametrize("style", ["blueprint", "band"])
def test_sharp_corners_use_a_box_radius_of_2_and_round_uses_8(style: str) -> None:
    assert 8.0 in _radii(_drawn(style, "round"))
    sharp = _radii(_drawn(style, "sharp"))
    assert 2.0 in sharp
    assert max(sharp) <= 2.0


def test_sharp_corners_make_edge_bends_smaller() -> None:
    def radius(svg: str) -> float:
        """The distance from the start of a bend (``L``) to its control point (``Q``)."""
        for path in ET.fromstring(svg).iter(f"{SVG_NS}path"):  # noqa: S314
            match = (
                re.search(r"L([\d.]+) ([\d.]+) Q([\d.]+) ([\d.]+)", path.get("d", ""))
                if "dag-edge" in path.get("class", "")
                else None
            )
            if match:
                ax, ay, cx, cy = map(float, match.groups())
                return abs(cx - ax) + abs(cy - ay)
        raise AssertionError("no bent edge")

    assert radius(_drawn("blueprint", "round")) > radius(_drawn("blueprint", "sharp"))
    assert radius(_drawn("blueprint", "sharp")) <= 8.0


def test_classic_ignores_corners() -> None:
    assert _drawn("classic", "round") == _drawn("classic", "sharp")


def test_the_corners_do_not_change_the_layout() -> None:
    """Both corner shapes put every box in the same place."""

    def boxes(svg: str) -> list[tuple[str, str, str, str]]:
        root = ET.fromstring(svg)  # noqa: S314
        return [
            (r.get("x", ""), r.get("y", ""), r.get("width", ""), r.get("height", ""))
            for r in root.iter(f"{SVG_NS}rect")
            if "dag-node" in r.get("class", "")
        ]

    assert boxes(_drawn("blueprint", "round")) == boxes(_drawn("blueprint", "sharp"))
