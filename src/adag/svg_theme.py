"""The stylesheet of the SVG that ``dag`` draws, built from ``--adag-*`` CSS custom properties.

Every colour lives in a class rule, never on an element, and every colour is
``var(--adag-name, default)``. A page that embeds the SVG inline sets ``--adag-*`` on any ancestor
and the graph follows. The light and dark sheets are the same rules with different defaults, so
they always style the same selectors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["CSS_VARIABLES", "CssVariable", "stylesheet"]


@dataclass(frozen=True)
class CssVariable:
    """One ``--adag-*`` custom property: its name, its default in each theme, and what it paints."""

    name: str
    light: str
    dark: str
    paints: str

    @property
    def css_name(self) -> str:
        return f"--adag-{self.name}"


def _v(name: str, light: str, dark: str, paints: str) -> CssVariable:
    return CssVariable(name, light, dark, paints)


_SANS = '-apple-system, "Segoe UI", system-ui, Helvetica, Arial, sans-serif'
_MONO = 'ui-monospace, "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace'

CSS_VARIABLES: tuple[CssVariable, ...] = (
    _v("canvas", "#ffffff", "#14161a", "The background behind the graph."),
    _v("card", "#ffffff", "#1c242d", "The body of a step in the band style."),
    _v("text", "#333333", "#e6e8eb", "Titles and other main text."),
    _v("muted", "#5f6368", "#a3aab2", "Descriptions, parameters and the cache key."),
    _v("edge", "#607d8b", "#8a96a3", "Edges and arrowheads (classic and band)."),
    _v("step-bg", "#f6f8fa", "#1d2127", "A step with no cache state (no `cache_dir`)."),
    _v("step-border", "#78909c", "#6b7683", "The border of a step with no cache state."),
    _v("neutral", "#64748b", "#8795a4", "The state square of a step with no cache state."),
    _v("cached", "#2a9456", "#4cc283", "The state square of a cached step."),
    _v("pending", "#c98e00", "#e3b341", "The state square of a pending step."),
    _v("blocked", "#d0413a", "#ef6b63", "The state square of a blocked step."),
    _v("cached-ink", "#1b6b3a", "#7dd9a5", "The word CACHED."),
    _v("pending-ink", "#8a6200", "#f0c75a", "The word PENDING."),
    _v("blocked-ink", "#b3261e", "#f5918b", "The word BLOCKED."),
    _v("cached-bg", "#e8f5e9", "#16261a", "A cached step: classic fill, band header."),
    _v("pending-bg", "#fff8e1", "#2a2317", "A pending step: classic fill, band header."),
    _v("blocked-bg", "#ffebee", "#2d1a1c", "A blocked step: classic fill, band header."),
    _v("cached-border", "#66bb6a", "#4f9a5b", "A cached step: classic and band border."),
    _v("pending-border", "#ffc107", "#b58900", "A pending step: classic and band border."),
    _v("blocked-border", "#ef5350", "#d05a5a", "A blocked step: classic and band border."),
    _v("leaf-bg", "#e8eaf6", "#1e2030", "A classic step with no inputs: fill."),
    _v("leaf-border", "#9fa8da", "#6b74a8", "A classic step with no inputs: border."),
    _v("input-bg", "#f3e5f5", "#271c2b", "An input node (parameter): fill."),
    _v("input-border", "#ce93d8", "#9a63a6", "An input node: border."),
    _v("input-ink", "#7b1fa2", "#d9a7e3", "An input name, and the input lines inside a step."),
    _v("file-bg", "#efebe9", "#241f1d", "A file input in the classic style: fill."),
    _v("file-border", "#bcaaa4", "#8a7466", "A file input in the classic style: border."),
    _v("file-ink", "#795548", "#cbb2a3", "A file input in the classic style: text."),
    _v("output-bg", "#e3f2fd", "#16232e", "The output node in the classic style: fill."),
    _v("output-border", "#64b5f6", "#4a90c4", "The output node in the classic style: border."),
    _v("output-ink", "#1565c0", "#9ecbf0", "The output node in the classic style: text."),
    _v("ink", "#1f3d8f", "#9db8ff", "Lines, boxes and text of the blueprint style."),
    _v("paper", "#eef3fc", "#0b1322", "The background of the blueprint style."),
    _v("grid", "#cfdaf0", "#17233b", "The grid lines of the blueprint style."),
    _v("mapped", "#8e24aa", "#c58ad6", "The dashed border of a fan-out node."),
    _v("loop", "#00897b", "#4db6ac", "The dotted border of a loop node."),
    _v("font", _SANS, _SANS, "The font of the text."),
    _v("font-mono", _MONO, _MONO, "The font of the blueprint style and of parameter lines."),
)

_BY_NAME = {v.name: v for v in CSS_VARIABLES}

# One rule set for both themes. ``@name`` becomes ``var(--adag-name, <default of the theme>)``.
# ``--st``, ``--st-ink``, ``--st-bg`` and ``--st-border`` are the per-node state colours: a node
# group sets them once and its shapes read them.
_RULES = """
.dag-canvas { fill: @canvas; }
.dag--blueprint .dag-canvas { fill: @paper; }
.dag-grid { fill: none; stroke: @grid; stroke-width: 1; }
.dag-text { fill: @text; font-family: @font; }
.dag--blueprint .dag-text, .dag-text--mono { font-family: @font-mono; }
.dag-text--desc, .dag-text--param, .dag-text--key, .dag-text--tab { fill: @muted; }
.dag-text--param, .dag-text--tab, .dag-text--key { font-family: @font-mono; }
.dag--blueprint .dag-text--title, .dag--blueprint .dag-text--desc,
.dag--blueprint .dag-text--param, .dag--blueprint .dag-text--key { fill: @ink; }
.dag-text--input { fill: @input-ink; font-family: @font-mono; }
.dag--blueprint .dag-text--input { fill: @ink; }
.dag-text--state { fill: var(--st-ink); font-weight: bold; letter-spacing: 0.05em; }
.dag-edge { fill: none; stroke: @edge; }
.dag-arrow { fill: @edge; }
.dag--blueprint .dag-edge { stroke: @ink; }
.dag--blueprint .dag-arrow { fill: @ink; }
.dag-item {
  --st: @neutral; --st-ink: @muted; --st-bg: @step-bg; --st-border: @step-border;
}
.dag-item.is-pending {
  --st: @pending; --st-ink: @pending-ink; --st-bg: @pending-bg; --st-border: @pending-border;
}
.dag-item.is-leaf {
  --st-bg: @leaf-bg; --st-border: @leaf-border;
}
.dag-item.is-cached {
  --st: @cached; --st-ink: @cached-ink; --st-bg: @cached-bg; --st-border: @cached-border;
}
.dag-item.is-blocked {
  --st: @blocked; --st-ink: @blocked-ink; --st-bg: @blocked-bg; --st-border: @blocked-border;
}
.dag--stateless .dag-item {
  --st: @neutral; --st-ink: @muted; --st-bg: @step-bg; --st-border: @step-border;
}
.dag-node { fill: var(--st-bg); stroke: var(--st-border); }
.dag--blueprint .dag-node { fill: @paper; stroke: @ink; }
.dag--band .dag-node { fill: @card; stroke: var(--st-border); }
.dag-head { fill: var(--st-bg); stroke: none; }
.dag-rule { stroke: var(--st-border); stroke-width: 2; }
.dag--blueprint .dag-rule { stroke: @ink; stroke-width: 1; opacity: 0.55; }
.dag-state { fill: var(--st); stroke: none; }
.dag-tab { fill: @card; stroke: var(--st-border); stroke-width: 1.5; }
.dag--blueprint .dag-tab { fill: @paper; stroke: @ink; }
.dag-node--param { fill: @input-bg; stroke: @input-border; }
.dag-node--file { fill: @file-bg; stroke: @file-border; }
.dag-node--output { fill: @output-bg; stroke: @output-border; }
.dag--blueprint .dag-node--param, .dag--blueprint .dag-node--file { fill: @paper; stroke: @ink; }
.dag--blueprint .dag-node--output { fill: @paper; stroke: @ink; stroke-dasharray: 5 3; }
.dag--band .dag-node--output { fill: @card; stroke: @edge; }
.dag-pin { fill: @ink; stroke: none; }
.dag-text--param-node { fill: @input-ink; }
.dag-text--file { fill: @file-ink; }
.dag-text--output { fill: @output-ink; }
.dag--blueprint .dag-text--param-node, .dag--blueprint .dag-text--file,
.dag--blueprint .dag-text--output { fill: @ink; }
.dag--band .dag-text--output { fill: @text; }
.dag .dag-node.dag-node--mapped { stroke-dasharray: 8 4; stroke: @mapped; }
.dag .dag-node.dag-node--loop { stroke-dasharray: 3 3; stroke: @loop; }
"""


_FLOW = """
@media (prefers-reduced-motion: no-preference) {
.dag-edge--flow { stroke-dasharray: 6 5; animation: dag-flow 1s linear infinite; }
@keyframes dag-flow { to { stroke-dashoffset: -11; } }
}
"""


def _theme_rules(*, use_dark: bool) -> str:
    def fill_in(match: re.Match[str]) -> str:
        variable = _BY_NAME[match.group(1)]
        default = variable.dark if use_dark else variable.light
        return f"var({variable.css_name}, {default})"

    return re.sub(r"@([a-z][a-z-]*[a-z])", fill_in, _RULES)


def stylesheet(theme: str = "light", *, animate: bool = False) -> str:
    """Return the CSS for ``theme``: ``"light"``, ``"dark"`` or ``"auto"`` (the browser's choice).

    ``"auto"`` is the light rules plus the dark rules inside ``prefers-color-scheme: dark``. Any
    other value is light. ``animate`` adds the moving dashes of ``SvgOptions(animate=True)``.
    """
    if theme == "auto":
        css = (
            _theme_rules(use_dark=False)
            + "\n@media (prefers-color-scheme: dark) {"
            + _theme_rules(use_dark=True)
            + "}\n"
        )
    else:
        css = _theme_rules(use_dark=theme == "dark")
    return css + _FLOW if animate else css
