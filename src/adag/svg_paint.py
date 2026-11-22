"""Measure and paint the nodes and edges of the graph SVG, in three styles and two densities.

``blueprint``: mono type, rounded corners, a faint grid, right-angle edges, a state square with the
state word and the cache key in the footer. ``band``: a header strip with the title and the state
word, curved edges. ``classic``: the first look of adag, centred text in rounded boxes.

Inputs (``InputParam`` and files) are separate small nodes (``terminals``), a tab on the top edge of
the step that reads them (``tabs``), or a line inside it (``rows``). In the compact density a step
is only its title, and its inputs are labels to its left.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from itertools import pairwise
from typing import Literal

from adag import svg_layout as lay
from adag.svg_theme import FONT_MONO, FONT_SANS

Style = Literal["blueprint", "band", "classic"]
Inputs = Literal["terminals", "tabs", "rows"]
Density = Literal["normal", "compact"]

STYLES: tuple[Style, ...] = ("blueprint", "band", "classic")
INPUTS: tuple[Inputs, ...] = ("terminals", "tabs", "rows")
DENSITIES: tuple[Density, ...] = ("normal", "compact")

_STATE_WORD = {"CACHED": "CACHED", "PENDING": "PENDING", "BLOCKED": "BLOCKED"}

_SANS_EM = 0.58
_BOLD_EM = 0.62
_MONO_EM = 0.62
_WORD_EM = 0.70  # the state word is bold, upper case and letter-spaced

_PAD_X = 14
_MAX_LINES = 6
_EPS = 0.5
_LANE_MARGIN = 10
_MAX_DESC_LINES = 3
_MAX_LABELS = 3
_BOX_R = 8  # blueprint: step boxes and the output box
_TERM_R = 6  # blueprint: input terminals and tabs
_LEGEND_HEIGHT = 34
_MONO_CLASSES = (
    "dag-text--param",
    "dag-text--tab",
    "dag-text--key",
    "dag-text--input",
)


@dataclass(frozen=True)
class Look:
    """The choices that decide how a graph is drawn."""

    style: Style = "blueprint"
    inputs: Inputs = "terminals"
    density: Density = "normal"
    stateless: bool = False


@dataclass(frozen=True)
class RNode:
    """What the painter shows for one node. ``inputs`` are the inputs folded into a step."""

    name: str
    kind: str
    title: str
    description: str = ""
    params: tuple[tuple[str, str], ...] = ()
    inputs: tuple[tuple[str, str], ...] = ()
    state: str = "PENDING"
    shape: str = ""
    key: str = ""
    value: str = ""
    is_leaf: bool = False
    is_final: bool = False


@dataclass(frozen=True)
class Tab:
    text: str
    width: float


@dataclass(frozen=True)
class Measured:
    """The size of a node and the text that fits in it."""

    width: float
    height: float
    title: str = ""
    desc: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    params: tuple[str, ...] = ()
    tabs: tuple[Tab, ...] = ()
    labels: tuple[str, ...] = ()
    left_extra: float = 0.0
    top_extra: float = 0.0
    arrive_lo: float = 0.0
    arrive_hi: float = 0.0


# ── Text ─────────────────────────────────────────────────────────────────────


def _text_width(text: str, size: float, em: float) -> int:
    return math.ceil(len(text) * size * em)


def _chars_that_fit(width: float, size: float, em: float, pad: float = 2 * _PAD_X) -> int:
    return max(1, int((width - pad) / (size * em)))


def _truncate(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max(1, max_chars - 3)] + "..."


def wrap_text_lines(text: str, max_width: int = 50) -> list[str]:
    """Wrap text into lines of at most ``max_width`` characters."""
    lines: list[str] = []
    current: list[str] = []
    length = 0
    for word in text.split():
        if length + len(word) + (1 if current else 0) > max_width and current:
            lines.append(" ".join(current))
            current = [word]
            length = len(word)
        else:
            current.append(word)
            length += len(word) + (1 if len(current) > 1 else 0)
    if current:
        lines.append(" ".join(current))
    return lines


def _wrap_capped(text: str, max_chars: int, max_lines: int) -> list[str]:
    lines = wrap_text_lines(text, max_chars)
    if len(lines) > max_lines:
        rest = " ".join(lines[max_lines - 1 :])
        lines = [*lines[: max_lines - 1], _truncate(rest, max_chars)]
    return lines


def _capped(lines: Sequence[str]) -> list[str]:
    """At most six lines: the sixth says how many more there are."""
    if len(lines) <= _MAX_LINES:
        return list(lines)
    return [*lines[: _MAX_LINES - 1], f"+{len(lines) - (_MAX_LINES - 1)} more"]


def _pair(name: str, value: str) -> str:
    return f"{name} = {value}" if value else name


def _state_word(node: RNode, look: Look) -> str:
    return "" if look.stateless else _STATE_WORD.get(node.state, "")


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(text: str) -> str:
    """Drop the control characters that XML cannot carry."""
    return _CONTROL.sub("", text)


def _num(value: float) -> str:
    """A coordinate with at most one decimal, so the same graph always gives the same text."""
    text = f"{round(value, 1):.1f}"
    return text.removesuffix(".0")


def tooltip(node: RNode, look: Look) -> str:
    """The full text of a node: what the box may have cut, plus the key."""
    first = node.title
    word = _state_word(node, look)
    if word:
        first += f" ({word.lower()})"
    lines = [first]
    if node.description:
        lines.append(node.description)
    if node.kind != "step" and node.value:
        lines.append(node.value)
    lines.extend(_pair(n, v) for n, v in node.inputs)
    lines.extend(_pair(n, v) for n, v in node.params)
    if node.key:
        lines.append(f"key {node.key}")
    return _clean("\n".join(lines))


# ── Measuring ────────────────────────────────────────────────────────────────


def measure(node: RNode, look: Look) -> Measured:
    """The size of ``node`` in ``look``, and the lines of text that fit in it."""
    if node.kind != "step":
        return _measure_input(node, look)
    if look.density == "compact":
        return _measure_compact(node, look)
    if look.style == "classic":
        return _measure_classic(node)
    if look.style == "band":
        return _measure_band(node, look)
    return _measure_blueprint(node, look)


def _tabs_for(node: RNode, look: Look, width: float, mark: str) -> tuple[Tab, ...]:
    if look.inputs != "tabs" or not node.inputs:
        return ()
    texts = [mark + _truncate(_pair(n, v), 28) for n, v in node.inputs][:_MAX_LINES]
    tabs: list[Tab] = []
    used = 0.0
    for raw in texts:
        # the first tab must fit the box
        fit = max(4, int((width - 8 - 22 - 6) / (11 * _MONO_EM)))
        text = raw if tabs else _truncate(raw, fit)
        w = _text_width(text, 11, _MONO_EM) + 22
        if used + w + 6 > width - 8 and tabs:
            break
        tabs.append(Tab(text, float(w)))
        used += w + 6
    hidden = len(node.inputs) - len(tabs)
    if hidden > 0:
        more = f"+{hidden} more"
        more_w = float(_text_width(more, 11, _MONO_EM) + 22)
        while len(tabs) > 1 and sum(t.width + 6 for t in tabs) + more_w > width - 8:
            tabs.pop()
            hidden += 1
            more = f"+{hidden} more"
            more_w = float(_text_width(more, 11, _MONO_EM) + 22)
        tabs.append(Tab(more, more_w))
    return tuple(tabs)


def _tab_arrival(width: float, tabs: Sequence[Tab]) -> tuple[float, float]:
    """Where edges may arrive when tabs sit on the top-left of the box: right of the tabs."""
    if not tabs:
        return 0.0, 0.0
    used = sum(t.width + 6 for t in tabs)
    lo = -width / 2 + min(used + 10, width * 0.6)
    hi = width / 2 - _PAD_X
    return lo, max(hi, lo + 24)


def _measure_blueprint(node: RNode, look: Look) -> Measured:
    word = _state_word(node, look)
    footer = bool(word or node.key)
    in_lines = [f"▸ {_pair(n, v)}" for n, v in node.inputs] if look.inputs == "rows" else []
    param_lines = [_pair(n, v) for n, v in node.params]
    natural = _text_width(node.title, 14, _BOLD_EM)
    if node.description:
        natural = max(natural, min(_text_width(node.description, 11, _MONO_EM), 300))
    for line in (*in_lines, *param_lines):
        natural = max(natural, _text_width(line, 11, _MONO_EM))
    need = natural + 2 * _PAD_X
    if footer:
        footer_w = 2 * _PAD_X + 14 + (_text_width(word, 11, _WORD_EM) if word else 0)
        if node.key:
            footer_w += 16 + _text_width(f"key {node.key}", 11, _MONO_EM)
        need = max(need, footer_w)
    width = float(min(max(need, 200), 340))
    fit = _chars_that_fit(width, 11, _MONO_EM)
    desc = _wrap_capped(node.description, fit, _MAX_DESC_LINES) if node.description else []
    rows = [_truncate(t, fit) for t in _capped(in_lines)]
    params = [_truncate(t, fit) for t in _capped(param_lines)]
    height = 12 + 20 + 12
    if desc:
        height += 4 + 16 * len(desc)
    if rows:
        height += 6 + 16 * len(rows)
    if params:
        height += 6 + 15 * len(params)
    if footer:
        height += 26
    tabs = _tabs_for(node, look, width, "▸ ")
    lo, hi = _tab_arrival(width, tabs)
    return Measured(
        width,
        float(height),
        title=_truncate(node.title, _chars_that_fit(width, 14, _BOLD_EM)),
        desc=tuple(desc),
        inputs=tuple(rows),
        params=tuple(params),
        tabs=tabs,
        top_extra=22.0 if tabs else 0.0,
        arrive_lo=lo,
        arrive_hi=hi,
    )


def _measure_band(node: RNode, look: Look) -> Measured:
    word = _state_word(node, look)
    in_lines = [_pair(n, v) for n, v in node.inputs] if look.inputs == "rows" else []
    param_lines = [_pair(n, v) for n, v in node.params]
    head = _text_width(node.title, 15, _BOLD_EM) + (
        _text_width(word, 11, _WORD_EM) + 16 if word else 0
    )
    natural = head
    if node.description:
        natural = max(natural, min(_text_width(node.description, 12, _SANS_EM), 300))
    for line in (*in_lines, *param_lines):
        natural = max(natural, _text_width(line, 11, _MONO_EM))
    width = float(min(max(natural + 2 * _PAD_X, 200), 320))
    fit_mono = _chars_that_fit(width, 11, _MONO_EM)
    desc = (
        _wrap_capped(node.description, _chars_that_fit(width, 12, _SANS_EM), _MAX_DESC_LINES)
        if node.description
        else []
    )
    rows = [_truncate(t, fit_mono) for t in _capped(in_lines)]
    params = [_truncate(t, fit_mono) for t in _capped(param_lines)]
    body = 20 + 16 * len(desc)
    if rows:
        body += (4 if desc else 0) + 15 * len(rows)
    if params:
        body += (4 if desc or rows else 0) + 15 * len(params)
    body = max(body, 36)
    title_room = width - 2 * _PAD_X - (_text_width(word, 11, _WORD_EM) + 12 if word else 0)
    tabs = _tabs_for(node, look, width, "")
    lo, hi = _tab_arrival(width, tabs)
    return Measured(
        width,
        float(30 + body),
        title=_truncate(node.title, max(4, int(title_room / (15 * _BOLD_EM)))),
        desc=tuple(desc),
        inputs=tuple(rows),
        params=tuple(params),
        tabs=tabs,
        top_extra=22.0 if tabs else 0.0,
        arrive_lo=lo,
        arrive_hi=hi,
    )


_CLASSIC_WIDTH = 380


def _measure_classic(node: RNode) -> Measured:
    height = 56 if node.description else 36
    desc: list[str] = []
    if node.description:
        desc = wrap_text_lines(node.description, int((_CLASSIC_WIDTH - 20) / 6.5))
        height = max(height, 14 + 6 + len(desc) * 14 + 16)
    params = [_pair(n, v) for n, v in node.params]
    if params:
        height += 6 + len(params) * 13 + 4
    return Measured(
        float(_CLASSIC_WIDTH),
        float(height),
        title=node.title,
        desc=tuple(desc),
        params=tuple(params),
    )


def _compact_labels(node: RNode) -> tuple[str, ...]:
    return tuple(_truncate(_pair(n, v), 28) for n, v in _capped_pairs(node.inputs))


def _capped_pairs(pairs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    if len(pairs) <= _MAX_LABELS:
        return list(pairs)
    return [*pairs[:2], (f"+{len(pairs) - 2} more", "")]


def _measure_compact(node: RNode, look: Look) -> Measured:
    word = _state_word(node, look)
    labels = _compact_labels(node)
    title = _truncate(node.title, 32)
    if look.style == "blueprint":
        width = float(max(_text_width(title, 13, _BOLD_EM) + (28 if word else 14) + 14, 100))
    elif look.style == "band":
        width = float(max(_text_width(title, 14, _BOLD_EM) + 28, 110))
    else:
        width = float(max(_text_width(title, 14, _SANS_EM) + 28, 100))
    height = float(max(34, 15 * len(labels) + 4))
    extra = float(max((_text_width(label, 11, _MONO_EM) for label in labels), default=0) + 24)
    return Measured(width, height, title=title, labels=labels, left_extra=extra if labels else 0.0)


def _measure_input(node: RNode, look: Look) -> Measured:
    if look.style == "classic":
        return Measured(float(max(len(node.title) * 8 + 32, 100)), 32.0, title=node.title)
    name = _truncate(node.title, 24)
    value = _truncate(node.value, 24) if node.value and node.kind == "param" else ""
    width = max(_text_width(name, 12, _MONO_EM), _text_width(value, 11, _MONO_EM)) + 28
    if look.style == "blueprint":
        width += 12
    return Measured(
        float(min(max(width, 80), 220)),
        38.0 if value else 30.0,
        title=name,
        desc=(value,) if value else (),
    )


def measure_output(label: str, look: Look) -> lay.LNode:
    """The output node: a box under the final step."""
    if look.style == "classic":
        return lay.LNode(lay.OUTPUT, float(max(len(label) * 8 + 32, 100)), 32.0, kind="output")
    return lay.LNode(
        lay.OUTPUT, float(max(_text_width(label, 12, _MONO_EM) + 32, 120)), 30.0, kind="output"
    )


# ── Painting ─────────────────────────────────────────────────────────────────


def _rect(
    parent: ET.Element, x: float, y: float, w: float, h: float, rx: float, cls: str, **a: str
) -> None:
    ET.SubElement(
        parent,
        "rect",
        {
            "x": _num(x),
            "y": _num(y),
            "width": _num(w),
            "height": _num(h),
            "rx": _num(rx),
            "class": cls,
            **a,
        },
    )


def _path(parent: ET.Element, d: str, cls: str, **a: str) -> None:
    ET.SubElement(parent, "path", {"d": d, "class": cls, **a})


def _text(
    parent: ET.Element,
    s: str,
    x: float,
    y: float,
    *,
    size: float,
    cls: str = "",
    anchor: str = "start",
    bold: bool = False,
    tip: str = "",
    mono: bool = False,
) -> None:
    """Add one line of text. The class sets the font and the colour; the attribute is the fallback."""
    use_mono = mono or any(name in cls.split() for name in _MONO_CLASSES)
    attrs = {
        "x": _num(x),
        "y": _num(y),
        "class": f"dag-text {cls}".strip(),
        "font-family": FONT_MONO if use_mono else FONT_SANS,
        "font-size": _num(size),
        "text-anchor": anchor,
        "dominant-baseline": "central",
    }
    if bold:
        attrs["font-weight"] = "bold"
    element = ET.SubElement(parent, "text", attrs)
    element.text = _clean(s)
    if tip:
        ET.SubElement(element, "title").text = _clean(tip)


_mono_text = partial(_text, mono=True)


def _group(parent: ET.Element, node: RNode, look: Look) -> ET.Element:
    state = "none" if look.stateless or node.kind != "step" else node.state.lower()
    classes = ["dag-item", f"dag-item--{node.kind}"]
    if node.kind == "step":
        classes.append(f"is-{node.state.lower()}")
        if node.is_leaf:
            classes.append("is-leaf")
    if node.shape:
        classes.append(f"dag-item--{node.shape}")
    group = ET.SubElement(
        parent,
        "g",
        {
            "class": " ".join(classes),
            "data-state": state,
            "data-kind": node.kind,
            "data-name": _clean(node.name),
        },
    )
    ET.SubElement(group, "title").text = tooltip(node, look)
    return group


def _box_classes(node: RNode) -> str:
    kind = "leaf" if node.is_leaf else "step"
    classes = f"dag-node dag-node--{kind} is-{node.state.lower()}"
    if node.shape:
        classes += f" dag-node--{node.shape}"
    return classes


def paint_node(parent: ET.Element, node: RNode, p: lay.Placed, m: Measured, look: Look) -> None:
    """Draw one node as a group with its tooltip."""
    group = _group(parent, node, look)
    if node.kind != "step":
        _paint_input(group, node, p, m, look)
    elif look.density == "compact":
        _paint_compact(group, node, p, m, look)
    elif look.style == "classic":
        _paint_classic(group, node, p, m)
    elif look.style == "band":
        _paint_band(group, node, p, m, look)
    else:
        _paint_blueprint(group, node, p, m, look)


def _paint_tabs(group: ET.Element, m: Measured, left: float, top: float, *, round_: bool) -> None:
    x = left
    for tab in m.tabs:
        if round_:
            d = (
                f"M{_num(x)} {_num(top)} V{_num(top - 14)} A8 8 0 0 1 {_num(x + 8)} {_num(top - 22)} "
                f"H{_num(x + tab.width - 8)} A8 8 0 0 1 {_num(x + tab.width)} {_num(top - 14)} V{_num(top)}"
            )
            _path(group, d, "dag-tab")
            _text(group, tab.text, x + 12, top - 10, size=11, cls="dag-text--tab")
        else:
            r = _TERM_R
            d = (
                f"M{_num(x)} {_num(top)} V{_num(top - 22 + r)} A{r} {r} 0 0 1 {_num(x + r)} {_num(top - 22)} "
                f"H{_num(x + tab.width - r)} A{r} {r} 0 0 1 {_num(x + tab.width)} {_num(top - 22 + r)} V{_num(top)}"
            )
            _path(group, d, "dag-tab")
            _text(group, tab.text, x + 10, top - 10, size=11, cls="dag-text--input")
        x += tab.width + 6


def _paint_blueprint(
    group: ET.Element, node: RNode, p: lay.Placed, m: Measured, look: Look
) -> None:
    x, y = p.x - p.width / 2, p.y - p.height / 2
    _paint_tabs(group, m, x + _BOX_R, y, round_=False)
    _rect(
        group,
        x,
        y,
        p.width,
        p.height,
        _BOX_R,
        _box_classes(node),
        **{"stroke-width": "2" if node.is_final else "1.5"},
    )
    _mono_text(
        group,
        m.title,
        x + _PAD_X,
        y + 22,
        size=14,
        cls="dag-text--title",
        bold=True,
        tip=node.title if m.title != node.title else "",
    )
    cursor = y + 32
    if m.desc:
        cursor += 4
        for line in m.desc:
            _mono_text(group, line, x + _PAD_X, cursor + 8, size=11, cls="dag-text--desc")
            cursor += 16
    if m.inputs:
        cursor += 6
        for line in m.inputs:
            _mono_text(group, line, x + _PAD_X, cursor + 8, size=11, cls="dag-text--input")
            cursor += 16
    if m.params:
        cursor += 6
        for line in m.params:
            _mono_text(group, line, x + _PAD_X, cursor + 7.5, size=11, cls="dag-text--param")
            cursor += 15
    word = _state_word(node, look)
    if word or node.key:
        top = y + p.height - 26
        ET.SubElement(
            group,
            "line",
            {
                "x1": _num(x),
                "y1": _num(top),
                "x2": _num(x + p.width),
                "y2": _num(top),
                "class": "dag-rule",
            },
        )
        mid = top + 13
        if word:
            _rect(group, x + _PAD_X, mid - 4, 8, 8, 0, "dag-state")
            _mono_text(group, word, x + _PAD_X + 14, mid, size=11, cls="dag-text--state")
        if node.key:
            _mono_text(
                group,
                f"key {node.key}",
                x + p.width - _PAD_X,
                mid,
                size=11,
                cls="dag-text--key",
                anchor="end",
            )


def _paint_band(group: ET.Element, node: RNode, p: lay.Placed, m: Measured, look: Look) -> None:
    x, y = p.x - p.width / 2, p.y - p.height / 2
    _paint_tabs(group, m, x, y, round_=True)
    _rect(
        group,
        x,
        y,
        p.width,
        p.height,
        8,
        _box_classes(node),
        **{"stroke-width": "2" if node.is_final else "1.5"},
    )
    d = (
        f"M{_num(x)} {_num(y + 8)} A8 8 0 0 1 {_num(x + 8)} {_num(y)} H{_num(x + p.width - 8)} "
        f"A8 8 0 0 1 {_num(x + p.width)} {_num(y + 8)} V{_num(y + 30)} H{_num(x)} Z"
    )
    _path(group, d, "dag-head")
    ET.SubElement(
        group,
        "line",
        {
            "x1": _num(x),
            "y1": _num(y + 30),
            "x2": _num(x + p.width),
            "y2": _num(y + 30),
            "class": "dag-rule",
        },
    )
    _text(
        group,
        m.title,
        x + _PAD_X,
        y + 15.5,
        size=15,
        cls="dag-text--title",
        bold=True,
        tip=node.title if m.title != node.title else "",
    )
    word = _state_word(node, look)
    if word:
        _text(
            group,
            word,
            x + p.width - _PAD_X,
            y + 15.5,
            size=11,
            cls="dag-text--state",
            anchor="end",
        )
    cursor = y + 30 + 10
    for line in m.desc:
        _text(group, line, x + _PAD_X, cursor + 8, size=12, cls="dag-text--desc")
        cursor += 16
    if m.inputs:
        cursor += 4 if m.desc else 0
        for line in m.inputs:
            _text(group, line, x + _PAD_X, cursor + 7.5, size=11, cls="dag-text--input")
            cursor += 15
    if m.params:
        cursor += 4 if (m.desc or m.inputs) else 0
        for line in m.params:
            _text(group, line, x + _PAD_X, cursor + 7.5, size=11, cls="dag-text--param")
            cursor += 15


def _paint_classic(group: ET.Element, node: RNode, p: lay.Placed, m: Measured) -> None:
    x, y = p.x - p.width / 2, p.y - p.height / 2
    _rect(
        group,
        x,
        y,
        p.width,
        p.height,
        10,
        _box_classes(node),
        **{"stroke-width": "2" if node.is_leaf else "1.5"},
    )
    line_height, param_line_height = 14, 13
    desc_h = len(m.desc) * line_height
    param_h = len(m.params) * param_line_height
    content_h = 14 + (6 + desc_h if m.desc else 0) + (6 + param_h if m.params else 0)
    title_y = p.y - content_h / 2 + 7
    _text(group, m.title, p.x, title_y, size=14, cls="dag-text--title", bold=True, anchor="middle")
    cursor = title_y + 7
    if m.desc:
        cursor += 6
        for i, line in enumerate(m.desc):
            _text(
                group,
                line,
                p.x,
                cursor + i * line_height + line_height / 2,
                size=10,
                cls="dag-text--desc",
                anchor="middle",
            )
        cursor += desc_h
    if m.params:
        cursor += 6
        for i, line in enumerate(m.params):
            _text(
                group,
                line,
                p.x,
                cursor + i * param_line_height + param_line_height / 2,
                size=9,
                cls="dag-text--param",
                anchor="middle",
            )


def _paint_compact(group: ET.Element, node: RNode, p: lay.Placed, m: Measured, look: Look) -> None:
    x, y = p.x - p.width / 2, p.y - p.height / 2
    word = _state_word(node, look)
    width = "2" if node.is_final else "1.5"
    if look.style == "blueprint":
        _rect(group, x, y, p.width, p.height, _BOX_R, _box_classes(node), **{"stroke-width": width})
        if word:
            _rect(group, x + 11, p.y - 4, 8, 8, 0, "dag-state")
        _mono_text(
            group, m.title, x + (28 if word else 14), p.y, size=13, cls="dag-text--title", bold=True
        )
        label_cls = "dag-text--input"
    elif look.style == "band":
        _rect(group, x, y, p.width, p.height, 8, _box_classes(node), **{"stroke-width": width})
        d = (
            f"M{_num(x)} {_num(y + 8)} A8 8 0 0 1 {_num(x + 8)} {_num(y)} H{_num(x + p.width - 8)} "
            f"A8 8 0 0 1 {_num(x + p.width)} {_num(y + 8)} V{_num(y + 9)} H{_num(x)} Z"
        )
        _path(group, d, "dag-head")
        _text(
            group, m.title, p.x, p.y + 4, size=14, cls="dag-text--title", bold=True, anchor="middle"
        )
        label_cls = "dag-text--input"
    else:
        _rect(group, x, y, p.width, p.height, 10, _box_classes(node), **{"stroke-width": width})
        _text(group, m.title, p.x, p.y, size=14, cls="dag-text--title", bold=True, anchor="middle")
        label_cls = "dag-text--param-node"
    count = len(m.labels)
    for i, label in enumerate(m.labels):
        ly = p.y + (i - (count - 1) / 2) * 15
        _text(group, label, x - 14, ly, size=11, cls=label_cls, anchor="end")
    if m.labels:
        ET.SubElement(
            group,
            "line",
            {
                "x1": _num(x - 12),
                "y1": _num(p.y),
                "x2": _num(x - 1),
                "y2": _num(p.y),
                "class": "dag-edge",
            },
        )


def _paint_input(group: ET.Element, node: RNode, p: lay.Placed, m: Measured, look: Look) -> None:
    x, y = p.x - p.width / 2, p.y - p.height / 2
    kind = node.kind if node.kind in ("param", "file") else "param"
    cls = f"dag-node dag-node--{kind}"
    if look.style == "classic":
        if kind == "file":
            _rect(group, x, y, p.width, p.height, 4, cls, **{"stroke-width": "1.5"})
            _text(group, m.title, p.x, p.y, size=11, cls="dag-text--file", anchor="middle")
            return
        ET.SubElement(
            group,
            "ellipse",
            {
                "cx": _num(p.x),
                "cy": _num(p.y),
                "rx": _num(p.width / 2),
                "ry": _num(p.height / 2),
                "class": cls,
                "stroke-width": "1.5",
            },
        )
        _text(group, m.title, p.x, p.y, size=12, cls="dag-text--param-node", anchor="middle")
        return
    blueprint = look.style == "blueprint"
    _rect(
        group,
        x,
        y,
        p.width,
        p.height,
        _TERM_R if blueprint else p.height / 2,
        cls,
        **{"stroke-width": "1.5"},
    )
    shift = 6.0 if blueprint else 0.0
    if blueprint:
        _rect(group, x + 8, p.y - 3, 6, 6, 0, "dag-pin")
    name_y = p.y - 7 if m.desc else p.y
    _mono_text(
        group,
        m.title,
        p.x + shift,
        name_y,
        size=12,
        cls="dag-text--param-node",
        bold=True,
        anchor="middle",
    )
    for line in m.desc:
        _mono_text(
            group, line, p.x + shift, p.y + 8, size=11, cls="dag-text--desc", anchor="middle"
        )


def paint_output(parent: ET.Element, label: str, p: lay.Placed, look: Look) -> None:
    group = ET.SubElement(
        parent,
        "g",
        {"class": "dag-item dag-item--output", "data-state": "none", "data-kind": "output"},
    )
    ET.SubElement(group, "title").text = _clean(label)
    x, y = p.x - p.width / 2, p.y - p.height / 2
    if look.style == "classic":
        _rect(
            group, x, y, p.width, p.height, 8, "dag-node dag-node--output", **{"stroke-width": "2"}
        )
        _text(group, label, p.x, p.y, size=12, cls="dag-text--output", bold=True, anchor="middle")
    elif look.style == "blueprint":
        _rect(
            group,
            x,
            y,
            p.width,
            p.height,
            _BOX_R,
            "dag-node dag-node--output",
            **{"stroke-width": "1.5"},
        )
        _mono_text(group, label, p.x, p.y, size=12, cls="dag-text--output", anchor="middle")
    else:
        _rect(
            group,
            x,
            y,
            p.width,
            p.height,
            8,
            "dag-node dag-node--output",
            **{"stroke-width": "1.5"},
        )
        _text(group, label, p.x, p.y, size=13, cls="dag-text--output", bold=True, anchor="middle")


# ── Edges ────────────────────────────────────────────────────────────────────


def _arrow_size(style: str) -> tuple[float, float]:
    return (8.0, 3.2) if style == "classic" else (9.0, 5.0)


def _end_points(route: lay.Route, arrow: float) -> list[lay.Point]:
    points = list(route.points)
    tip_x, tip_y = points[-1]
    points[-1] = (tip_x, tip_y - arrow)
    return points


def curve_path(route: lay.Route, arrow: float) -> str:
    """Cubic Béziers through the route, vertical at every point, so a long edge bends smoothly.

    An edge that crosses a row (a virtual point) runs straight down through the whole row first, so
    it never sweeps under a box of that row.
    """
    pts = _end_points(route, arrow)
    expanded: list[lay.Point] = []
    for point, row in zip(pts, route.rows or (None,) * len(pts), strict=False):
        if row is None:
            expanded.append(point)
        else:
            expanded.extend([(point[0], row[0] - _LANE_MARGIN), (point[0], row[1] + _LANE_MARGIN)])
    parts = [f"M{_num(expanded[0][0])} {_num(expanded[0][1])}"]
    for (x0, y0), (x1, y1) in pairwise(expanded):
        if abs(x0 - x1) < _EPS:
            parts.append(f"L{_num(x1)} {_num(y1)}")
            continue
        k = (y1 - y0) * 0.5
        parts.append(f"C{_num(x0)} {_num(y0 + k)} {_num(x1)} {_num(y1 - k)} {_num(x1)} {_num(y1)}")
    return " ".join(parts)


def ortho_path(route: lay.Route, arrow: float, corner: float) -> str:
    """Right angles with rounded corners: down, sideways in the gap, down."""
    pts = _end_points(route, arrow)
    vertices: list[lay.Point] = [pts[0]]
    for i, (a, b) in enumerate(pairwise(pts)):
        if abs(a[0] - b[0]) > _EPS:
            jy = route.jogs[i]
            vertices.extend([(a[0], jy), (b[0], jy)])
        vertices.append(b)
    parts = [f"M{_num(vertices[0][0])} {_num(vertices[0][1])}"]
    for i in range(1, len(vertices) - 1):
        prev, cur, nxt = vertices[i - 1], vertices[i], vertices[i + 1]
        if (prev[0] == cur[0] == nxt[0]) or (prev[1] == cur[1] == nxt[1]):
            continue
        d1 = math.hypot(cur[0] - prev[0], cur[1] - prev[1])
        d2 = math.hypot(nxt[0] - cur[0], nxt[1] - cur[1])
        r = min(corner, d1 / 2, d2 / 2)
        ax = cur[0] - (cur[0] - prev[0]) / d1 * r
        ay = cur[1] - (cur[1] - prev[1]) / d1 * r
        bx = cur[0] + (nxt[0] - cur[0]) / d2 * r
        by = cur[1] + (nxt[1] - cur[1]) / d2 * r
        parts.append(f"L{_num(ax)} {_num(ay)} Q{_num(cur[0])} {_num(cur[1])} {_num(bx)} {_num(by)}")
    parts.append(f"L{_num(vertices[-1][0])} {_num(vertices[-1][1])}")
    return " ".join(parts)


def paint_route(
    parent: ET.Element,
    route: lay.Route,
    look: Look,
    settings: lay.LayoutSettings,
    *,
    flow: bool = False,
) -> None:
    arrow, half = _arrow_size(look.style)
    d = (
        ortho_path(route, arrow, settings.corner)
        if look.style == "blueprint"
        else curve_path(route, arrow)
    )
    cls = "dag-edge dag-edge--output" if route.dashed else "dag-edge"
    if flow:
        cls += " dag-edge--flow"
    attrs = {"stroke-width": "1.5", "stroke-linejoin": "round", "stroke-linecap": "round"}
    if route.dashed:
        attrs["stroke-dasharray"] = "6,4"
    _path(parent, d, cls, **attrs)
    tip_x, tip_y = route.points[-1]
    points = (
        f"{_num(tip_x - half)},{_num(tip_y - arrow)} {_num(tip_x + half)},{_num(tip_y - arrow)} "
        f"{_num(tip_x)},{_num(tip_y)}"
    )
    ET.SubElement(
        parent,
        "polygon",
        {"points": points, "class": "dag-arrow dag-arrow--output" if route.dashed else "dag-arrow"},
    )


def settings_for(look: Look) -> lay.LayoutSettings:
    """Distances per style: right angles need more room in the gap between rows."""
    if look.density == "compact":
        return lay.LayoutSettings(gap_y=56.0, gap_x=30.0)
    return lay.LayoutSettings()


# ── The whole graph ──────────────────────────────────────────────────────────


def _fold_inputs(
    nodes: Sequence[RNode], edges: Sequence[tuple[str, str]], *, fold: bool
) -> tuple[list[RNode], list[tuple[str, str]]]:
    """With ``fold``, move inputs into the steps that read them.

    Only an input (a param or a file with no edge into it) that feeds steps and nothing else is
    folded. Any other node stays a node, and so does an input that nothing reads.
    """
    if not fold:
        return list(nodes), list(edges)
    by_name = {n.name: n for n in nodes}
    has_input = {target for _, target in edges}
    readers: dict[str, list[str]] = {}
    for source, target in edges:
        readers.setdefault(source, []).append(target)
    foldable = {
        n.name
        for n in nodes
        if n.kind in ("param", "file")
        and n.name not in has_input
        and readers.get(n.name)
        and all(by_name.get(t) is not None and by_name[t].kind == "step" for t in readers[n.name])
    }
    folded: dict[str, list[tuple[str, str]]] = {}
    kept: list[tuple[str, str]] = []
    for source, target in edges:
        if source in foldable:
            src = by_name[source]
            entry = (src.title, src.value if src.kind == "param" else "")
            if entry not in folded.setdefault(target, []):
                folded[target].append(entry)
        else:
            kept.append((source, target))
    out = [
        _with_inputs(n, tuple(folded.get(n.name, ()))) if n.kind == "step" else n
        for n in nodes
        if n.name not in foldable
    ]
    return out, kept


def _with_inputs(node: RNode, inputs: tuple[tuple[str, str], ...]) -> RNode:
    return RNode(
        node.name,
        node.kind,
        node.title,
        node.description,
        node.params,
        inputs,
        node.state,
        node.shape,
        node.key,
        node.value,
        node.is_leaf,
        node.is_final,
    )


def draw(
    nodes: Sequence[RNode],
    edges: Sequence[tuple[str, str]],
    *,
    look: Look,
    output_label: str = "",
    final: str | None = None,
    css: str | None = None,
    background: bool = True,
    legend: bool = False,
    animate: bool = False,
) -> str:
    """Return the SVG text of the graph."""
    fold = look.style != "classic" and (look.inputs != "terminals" or look.density == "compact")
    if look.style == "classic" and look.density == "compact":
        fold = True
    shown, shown_edges = _fold_inputs(nodes, edges, fold=fold)
    measured = {n.name: measure(n, look) for n in shown}
    sizes = [
        lay.LNode(
            n.name,
            measured[n.name].width,
            measured[n.name].height,
            measured[n.name].left_extra,
            measured[n.name].top_extra,
            measured[n.name].arrive_lo,
            measured[n.name].arrive_hi,
            n.kind,
        )
        for n in shown
    ]
    settings = settings_for(look)
    has_output = bool(output_label) and final is not None
    result = lay.layout(
        sizes,
        shown_edges,
        output=measure_output(output_label, look) if has_output else None,
        final=final,
        settings=lay.LayoutSettings(
            gap_y=settings.gap_y, gap_x=settings.gap_x, arrow=_arrow_size(look.style)[0]
        ),
    )

    legend_states = _legend_states(shown, look) if legend else []
    canvas_h = result.height + (_LEGEND_HEIGHT if legend_states else 0)
    canvas_w = max(result.width, _legend_width(legend_states) + 2 * lay.LayoutSettings().margin)
    root_class = f"dag dag--{look.style}"
    if look.density == "compact":
        root_class += " dag--compact"
    if look.stateless:
        root_class += " dag--stateless"
    svg = ET.Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "class": root_class,
            "width": _num(canvas_w),
            "height": _num(canvas_h),
            "viewBox": f"0 0 {_num(canvas_w)} {_num(canvas_h)}",
        },
    )
    if css is not None:
        ET.SubElement(svg, "style").text = css
    if background:
        ET.SubElement(
            svg,
            "rect",
            {"class": "dag-canvas", "width": _num(canvas_w), "height": _num(canvas_h)},
        )
        if look.style == "blueprint":
            _path(svg, _grid(canvas_w, canvas_h), "dag-grid")
    pending = {n.name for n in shown if n.state == "PENDING" and not look.stateless}
    for route in result.routes:
        paint_route(svg, route, look, settings, flow=animate and route.target in pending)
    for n in shown:
        paint_node(svg, n, result.nodes[n.name], measured[n.name], look)
    if has_output and lay.OUTPUT in result.nodes:
        paint_output(svg, output_label, result.nodes[lay.OUTPUT], look)
    if legend_states:
        _paint_legend(svg, legend_states, result.height + _LEGEND_HEIGHT / 2)
    return ET.tostring(svg, encoding="unicode")


def _legend_states(shown: Sequence[RNode], look: Look) -> list[str]:
    """The states drawn in this graph, in a fixed order. None for a graph with no state."""
    if look.stateless:
        return []
    present = {n.state for n in shown if n.kind == "step"}
    return [word for word in _STATE_WORD if word in present]


def _legend_width(states: Sequence[str]) -> float:
    return (
        sum(14 + _text_width(state, 11, _WORD_EM) + 24 for state in states) - 24 if states else 0.0
    )


def _paint_legend(parent: ET.Element, states: Sequence[str], y: float) -> None:
    """One row of state squares and words, left aligned under the graph."""
    group = ET.SubElement(parent, "g", {"class": "dag-legend"})
    x = 30.0
    for state in states:
        item = ET.SubElement(
            group,
            "g",
            {
                "class": f"dag-item dag-item--step dag-legend-item is-{state.lower()}",
                "data-state": state.lower(),
            },
        )
        _rect(item, x, y - 4, 8, 8, 0, "dag-state")
        _text(item, state, x + 14, y, size=11, cls="dag-text--state")
        x += 14 + _text_width(state, 11, _WORD_EM) + 24


def _grid(width: float, height: float, step: int = 20) -> str:
    parts = [f"M{x} 0V{_num(height)}" for x in range(step, int(width), step)]
    parts += [f"M0 {y}H{_num(width)}" for y in range(step, int(height), step)]
    return "".join(parts)
