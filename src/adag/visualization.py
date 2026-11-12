"""DAG visualization: walk the Deferred graph and render it as SVG.

tokens = tokenizer(text=FileSource("data.txt"))
embeddings = encoder(tokens=tokens)

dag(embeddings)                  # returns an SVG object; str() gives the text
dag(embeddings, path="out.svg")  # saves to a file
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import structlog

from adag.deferred import Deferred, InputParam
from adag.io import FileSource, atomic_write_text
from adag.step import Step, StepResult

logger = structlog.get_logger()


class SVG:
    """Wrapper around an SVG string that Jupyter renders as an image."""

    def __init__(self, source: str) -> None:
        self.source = source

    def _repr_svg_(self) -> str:
        return self.source

    def __str__(self) -> str:
        return self.source


# ── Graph model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Node:
    """A node in the walked graph.

    `name` is the node's unique KEY (edges reference it, layout positions by it). `title`, when set, is the
    human display label; empty → the renderer derives one from `name`. A Step may expose a per-INSTANCE label
    via a `node_label` attribute (walk() then uses it as both name and title; the name gets a suffix on collision) so
    two instances of the SAME Step class render as two distinct boxes instead of collapsing into one.
    """

    name: str
    step: Step[Any] | None
    kind: str
    title: str = ""
    result: StepResult[Any] | None = None  # the graph node; its cache key covers the inputs


@dataclass(frozen=True)
class Edge:
    """A directed edge in the walked graph."""

    source: str
    target: str
    label: str


# Colours live in the stylesheet, not on the elements: every shape carries a
# class instead of a fill, so a host page can retheme the graph (dark mode,
# brand palette) without the renderer knowing anything about it. Geometry and
# font sizes stay as attributes because the layout maths is computed from them.
_STYLESHEET_LIGHT = """
.dag-canvas { fill: #ffffff; }
.dag-edge { fill: none; stroke: #90a4ae; }
.dag-arrow { fill: #90a4ae; }
.dag-node--step { fill: #fff8e1; stroke: #ffc107; }
.dag-node--leaf { fill: #e8eaf6; stroke: #9fa8da; }
.dag-node--step.is-cached, .dag-node--leaf.is-cached { fill: #e8f5e9; stroke: #66bb6a; }
.dag-node--step.is-blocked, .dag-node--leaf.is-blocked { fill: #ffebee; stroke: #ef5350; }
.dag-node--param { fill: #f3e5f5; stroke: #ce93d8; }
.dag-node--file { fill: #efebe9; stroke: #bcaaa4; }
.dag-node--output { fill: #e3f2fd; stroke: #64b5f6; }
.dag-text { fill: #333333; }
.dag-text--desc { fill: #777777; }
.dag-text--param { fill: #5a5a5a; }
.dag-text--param-node { fill: #7b1fa2; }
.dag-text--file { fill: #795548; }
.dag-text--output { fill: #1565c0; }
"""

_STYLESHEET_DARK = """
.dag-canvas { fill: #14161a; }
.dag-edge { fill: none; stroke: #6b7683; }
.dag-arrow { fill: #6b7683; }
.dag-node--step { fill: #2a2317; stroke: #b58900; }
.dag-node--leaf { fill: #1e2030; stroke: #6b74a8; }
.dag-node--step.is-cached, .dag-node--leaf.is-cached { fill: #16261a; stroke: #4f9a5b; }
.dag-node--step.is-blocked, .dag-node--leaf.is-blocked { fill: #2d1a1c; stroke: #d05a5a; }
.dag-node--param { fill: #271c2b; stroke: #9a63a6; }
.dag-node--file { fill: #241f1d; stroke: #8a7466; }
.dag-node--output { fill: #16232e; stroke: #4a90c4; }
.dag-text { fill: #e6e8eb; }
.dag-text--desc { fill: #9aa1a9; }
.dag-text--param { fill: #b6bcc4; }
.dag-text--param-node { fill: #d9a7e3; }
.dag-text--file { fill: #cbb2a3; }
.dag-text--output { fill: #9ecbf0; }
"""

_STYLESHEETS = {"light": _STYLESHEET_LIGHT, "dark": _STYLESHEET_DARK}


@dataclass(frozen=True)
class SvgOptions:
    """What the renderer emits.

    ``include_styles=False`` leaves the SVG unpainted apart from its classes,
    for a host that supplies the whole stylesheet. ``show_params=False`` drops
    the per-step parameter lines, which also shrinks the boxes: the layout is
    computed from the same node data that gets drawn.
    """

    theme: str = "light"
    include_styles: bool = True
    include_background: bool = True
    show_params: bool = True

    def stylesheet(self) -> str:
        return _STYLESHEETS.get(self.theme, _STYLESHEET_LIGHT)


_DEFAULT_SVG_OPTIONS = SvgOptions()


@dataclass(frozen=True)
class NodeMetadata:
    """Pre-extracted display info for a graph node, decoupled from Step introspection."""

    name: str
    kind: str
    title: str
    description: str = ""
    params: tuple[tuple[str, str], ...] = ()
    state: str = "PENDING"


def _node_name_and_kind(deferred: Deferred[Any]) -> tuple[str, str] | None:
    """Return (name, kind) for a leaf Deferred, or None for StepResult."""
    if isinstance(deferred, FileSource):
        return Path(deferred.path).name, "file"
    if isinstance(deferred, InputParam):
        return deferred.name, "param"
    return None


def walk(output: Deferred[Any]) -> tuple[list[Node], list[Edge]]:
    """Walk any Deferred backward and return the full dependency graph.

    Step nodes are identified by instance (``id(step)``), not class name. A shared upstream (the
    same instance reached on two paths) is one node. Two distinct instances of the same class are
    two nodes. Each node's label is its ``node_label`` attribute when present, else the class name.
    The key gets a suffix (``Name#2``) only when two instances would otherwise collide.
    """
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    placed: dict[int, str] = {}  # id(step) → the unique node name assigned to that instance

    def _visit(deferred: Any) -> str:
        leaf = _node_name_and_kind(deferred)
        if leaf is not None:
            name, kind = leaf
            if name not in nodes:
                nodes[name] = Node(name=name, step=None, kind=kind)
            return name

        # Capture .step while deferred is still Any (before isinstance narrows it).
        step: Any = deferred.step if hasattr(deferred, "step") else None

        if isinstance(deferred, StepResult):
            if id(step) in placed:  # same instance reached again (shared upstream) → the same node
                return placed[id(step)]
            label = getattr(step, "node_label", "") or ""
            base = label or type(step).__name__
            name = base
            suffix = 2
            while (
                name in nodes
            ):  # a DIFFERENT instance wants the same base name → disambiguate the key
                name = f"{base}#{suffix}"
                suffix += 1
            placed[id(step)] = name
            nodes[name] = Node(
                name=name,
                step=step,
                kind="step",
                title=label,
                result=cast("StepResult[Any]", deferred),
            )
            for input_name, source in deferred.sources.items():
                if isinstance(source, Deferred):
                    source_name = _visit(source)
                    if source_name:
                        edges.append(Edge(source=source_name, target=name, label=input_name))
            return name

        return ""

    _visit(output)
    return list(nodes.values()), edges


# ── Shared utilities ─────────────────────────────────────────────────────────


def compute_state(node: Node, cache_dir: Path | None) -> str:
    """Return the state a node is drawn with: ``CACHED``, ``PENDING``, ``BLOCKED`` or ``SOURCE``.

    ``SOURCE`` is an ``InputParam`` or a file. ``BLOCKED`` is a step with no hash, for example a
    ``FileSource`` field whose file is missing. Without ``cache_dir`` every step is ``PENDING``.
    The key is computed with ``InputParam`` defaults, not with the values of a run.
    """
    if node.step is None:
        return "SOURCE"
    if cache_dir is None:
        return "PENDING"
    h = node.step.content_hash() if hasattr(node.step, "content_hash") else None
    if h is None:
        return "BLOCKED"
    if node.result is not None:
        # The pipeline caches under the node key (step and inputs), not the step hash.
        return "CACHED" if node.result.is_cached(cache_dir) else "PENDING"
    is_cached_attr: object = getattr(node.step, "is_cached", None)
    if is_cached_attr is not None:
        cached = is_cached_attr(cache_dir) if callable(is_cached_attr) else is_cached_attr
        return "CACHED" if cached else "PENDING"
    return "PENDING"


def _camel_to_words(name: str) -> str:
    title = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    title = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", title)
    return title.strip()


def _first_doc_line(step: Step[Any]) -> str:
    """First sentence of docstring, or label/description class attribute."""
    for cls in type(step).__mro__:
        if cls is Step or cls is object:
            break
        doc = getattr(cls, "__doc__", None)
        if doc:
            return doc.strip().split("\n")[0].strip().rstrip(".")
    return getattr(type(step), "description", "") or getattr(type(step), "label", "")


def _scalar_params(step: Step[Any], max_str_len: int = 30) -> list[tuple[str, str]]:
    """Extract scalar dataclass fields (int, float, bool, short str) for display."""
    items: list[tuple[str, str]] = []
    for f in dataclasses.fields(step):
        if (
            f.name == "node_label"
        ):  # the box's display label (see walk()), not a step parameter to list
            continue
        value = getattr(step, f.name)
        if isinstance(value, bool):
            items.append((f.name, str(value)))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            items.append((f.name, f"{value:g}" if isinstance(value, float) else str(value)))
        elif isinstance(value, str) and 0 < len(value) <= max_str_len:
            items.append((f.name, value))
        elif value is None:
            items.append((f.name, "None"))
    return items


def _wrap_text_lines(text: str, max_width: int = 50) -> list[str]:
    """Wrap text into lines of at most max_width characters."""
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
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


# ── Entry point ──────────────────────────────────────────────────────────────


def dag(
    output: Deferred[Any],
    *,
    cache_dir: Path | None = None,
    path: str | Path | None = None,
    prefix: str = "",
    options: SvgOptions = _DEFAULT_SVG_OPTIONS,
) -> SVG:
    """Render the DAG as SVG.

    Args:
        output: Any Deferred: walks backward to discover the full graph.
        cache_dir: Cache directory (for cached/pending state coloring).
        path: Save SVG to this path.
        prefix: Text removed from the start of step names in titles.
        options: Theme, and whether to embed the stylesheet, paint a background
            or draw the per-step parameters.
    """
    graph_nodes, graph_edges = walk(output)
    return SVG(
        _render_svg(
            output,
            graph_nodes,
            graph_edges,
            cache_dir=cache_dir,
            prefix=prefix,
            path=path,
            options=options,
        )
    )


# ── Graphviz renderer ────────────────────────────────────────────────────────


# ── Layout ───────────────────────────────────────────────────────────────────


@dataclass
class LayoutNode:
    """A positioned node for rendering."""

    name: str
    kind: str
    title: str
    description: str
    state: str
    is_leaf: bool
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    params: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


def _orient(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    """Signed area of triangle (a, b, c): positive for a left turn, negative for a right turn."""
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_cross(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> bool:
    """True when the two line segments properly intersect (shared endpoints are handled by the caller)."""
    ax, ay, bx, by = first
    cx, cy, dx, dy = second
    side_c = _orient(ax, ay, bx, by, cx, cy)
    side_d = _orient(ax, ay, bx, by, dx, dy)
    side_a = _orient(cx, cy, dx, dy, ax, ay)
    side_b = _orient(cx, cy, dx, dy, bx, by)
    return (side_c > 0) != (side_d > 0) and (side_a > 0) != (side_b > 0)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _normalized_positions(ordered_levels: dict[int, list[str]]) -> dict[str, float]:
    """Map every node to a [0, 1] coordinate based on its index within its level's ordering."""
    positions: dict[str, float] = {}
    for names in ordered_levels.values():
        count = len(names)
        for index, name in enumerate(names):
            positions[name] = 0.5 if count == 1 else index / (count - 1)
    return positions


def _count_edge_crossings(
    ordered_levels: dict[int, list[str]], node_y: dict[str, float], edge_list: list[tuple[str, str]]
) -> int:
    """Count edge pairs that cross given the current per-level ordering. Edges sharing an endpoint never count."""
    positions = _normalized_positions(ordered_levels)

    segments: list[tuple[str, str, float, float, float, float]] = []
    for source, target in edge_list:
        if source in positions and target in positions:
            segments.append(
                (
                    source,
                    target,
                    positions[source],
                    node_y[source],
                    positions[target],
                    node_y[target],
                )
            )

    crossings = 0
    for first_index in range(len(segments)):
        first = segments[first_index]
        for second_index in range(first_index + 1, len(segments)):
            second = segments[second_index]
            if first[0] in (second[0], second[1]) or first[1] in (second[0], second[1]):
                continue
            if _segments_cross(
                (first[2], first[3], first[4], first[5]),
                (second[2], second[3], second[4], second[5]),
            ):
                crossings += 1
    return crossings


def _reorder_level(
    ordered_levels: dict[int, list[str]], lvl: int, neighbors_of: dict[str, list[str]]
) -> None:
    """Reorder one level by the median position of each node's neighbors on the adjacent side."""
    positions = _normalized_positions(ordered_levels)
    names = ordered_levels[lvl]
    count = len(names)

    keyed: list[tuple[float, int, str]] = []
    for index, name in enumerate(names):
        neighbor_positions = [
            positions[neighbor] for neighbor in neighbors_of.get(name, []) if neighbor in positions
        ]
        fallback = 0.5 if count == 1 else index / (count - 1)
        key = _median(neighbor_positions) if neighbor_positions else fallback
        keyed.append((key, index, name))

    keyed.sort()
    ordered_levels[lvl] = [name for _, _, name in keyed]


def _minimize_crossings(
    levels: dict[int, list[str]],
    inputs_of: dict[str, list[str]],
    consumers_of: dict[str, list[str]],
    node_y: dict[str, float],
    edge_list: list[tuple[str, str]],
    *,
    iterations: int = 4,
) -> dict[int, list[str]]:
    """Sugiyama-style ordering pass: alternate down/up median sweeps and keep the lowest-crossing ordering.

    This decides only the per-level column ORDER, not coordinates. A down sweep orders each level by the
    median position of its inputs above; an up sweep orders by the median position of its consumers below.
    Because median positions are normalized per level, the heuristic also handles edges that skip levels.
    """
    ordered_levels = {lvl: list(names) for lvl, names in levels.items()}
    sorted_levels = sorted(ordered_levels.keys())
    if len(sorted_levels) < 2:
        return ordered_levels

    best_order = {lvl: list(names) for lvl, names in ordered_levels.items()}
    best_crossings = _count_edge_crossings(best_order, node_y, edge_list)

    for _ in range(iterations):
        for lvl in sorted_levels[1:]:
            _reorder_level(ordered_levels, lvl, inputs_of)
        down_crossings = _count_edge_crossings(ordered_levels, node_y, edge_list)
        if down_crossings < best_crossings:
            best_crossings = down_crossings
            best_order = {lvl: list(names) for lvl, names in ordered_levels.items()}

        for lvl in reversed(sorted_levels[:-1]):
            _reorder_level(ordered_levels, lvl, consumers_of)
        up_crossings = _count_edge_crossings(ordered_levels, node_y, edge_list)
        if up_crossings < best_crossings:
            best_crossings = up_crossings
            best_order = {lvl: list(names) for lvl, names in ordered_levels.items()}

    return best_order


def _compute_layout(
    nodes: dict[str, NodeMetadata],
    edges: list[tuple[str, str]],
    *,
    output_label: str = "",
    final_step_name: str | None = None,
    step_width: float = 380,
    step_height_with_desc: float = 56,
    step_height_no_desc: float = 36,
    small_width: float = 100,
    small_height: float = 32,
    y_gap: float = 60,
    x_gap: float = 40,
) -> tuple[list[LayoutNode], list[tuple[LayoutNode, LayoutNode]]]:
    """Constraint-based layout.

    1. Assign levels (Y groups) by walking backward from output
    2. Push leaf inputs as late as possible
    3. Single-input chains share the same X
    4. Multi-input nodes center under their inputs
    5. Adaptive Y gaps for multi-input levels
    """
    targets = {tgt for _, tgt in edges}

    inputs_of: dict[str, list[str]] = {n: [] for n in nodes}
    consumers_of: dict[str, list[str]] = {n: [] for n in nodes}
    for source, target in edges:
        inputs_of[target].append(source)
        consumers_of[source].append(target)

    all_names = list(nodes.keys())
    has_output = bool(output_label) and final_step_name is not None
    if has_output:
        assert final_step_name is not None
        all_names.append("__output__")
        inputs_of["__output__"] = [final_step_name]
        consumers_of.setdefault(final_step_name, []).append("__output__")

    def node_size(name: str) -> tuple[float, float]:
        if name == "__output__":
            output_w = max(len(output_label) * 8 + 32, small_width) if output_label else small_width
            return output_w, small_height
        meta = nodes.get(name)
        if meta is not None and meta.kind == "step":
            desc = meta.description
            params = meta.params
            h = step_height_with_desc if desc else step_height_no_desc
            if desc:
                max_chars = int((step_width - 20) / 6.5)
                desc_lines = _wrap_text_lines(desc, max_chars)
                line_height = 14
                total_text_height = 14 + 6 + len(desc_lines) * line_height
                h = max(h, total_text_height + 16)
            if params:
                h += 6 + len(params) * 13 + 4
            return step_width, h
        title = meta.title if meta is not None else name
        w = max(len(title) * 8 + 32, small_width)
        return w, small_height

    # Phase 1: Assign levels (longest path from roots)
    level: dict[str, int] = {}

    def assign_level(name: str) -> int:
        if name in level:
            return level[name]
        deps = inputs_of.get(name, [])
        if not deps:
            level[name] = 0
            return 0
        level[name] = max(assign_level(d) for d in deps) + 1
        return level[name]

    for name in all_names:
        assign_level(name)

    # Phase 2: Push leaf nodes as late as possible
    for name in all_names:
        if inputs_of.get(name):
            continue
        consumer_list = consumers_of.get(name, [])
        if consumer_list:
            level[name] = min(level[c] for c in consumer_list) - 1

    # Phase 3: Assign Y coordinates with adaptive gaps that respect actual box heights.
    # Tall step boxes (many scalar params) would otherwise collide with the next row
    # because the old base_gap was a single constant regardless of who's where.
    max_level_val = max(level.values()) if level else 0

    edge_list = list(edges)
    if has_output:
        assert final_step_name is not None
        edge_list.append((final_step_name, "__output__"))

    # Per-level max box height, the row's vertical footprint.
    level_height: dict[int, float] = {}
    for lvl in range(max_level_val + 1):
        heights = [node_size(name)[1] for name in all_names if level.get(name) == lvl]
        level_height[lvl] = max(heights) if heights else small_height

    # Edges that pass THROUGH a level need room, and room is area (height x width).
    # Adding only height stretches those edges without separating them, parallel
    # through-edges stay stacked no matter how tall the gap. So split the budget:
    # a capped vertical gap here for a little breathing room, plus extra horizontal
    # spacing below (driven by the busiest level) so the through-edges get real
    # lateral lanes instead of overlapping.
    crossing_at_level: dict[int, int] = {}
    for lvl in range(max_level_val + 1):
        crossing_edges = 0
        for src_name, tgt_name in edge_list:
            src_lvl = level.get(src_name, -1)
            tgt_lvl = level.get(tgt_name, -1)
            if src_lvl <= lvl < tgt_lvl:
                crossing_edges += 1
        crossing_at_level[lvl] = crossing_edges

    peak_crossing = max(crossing_at_level.values()) if crossing_at_level else 0

    # Cap the vertical penalty so a single busy level can't run the canvas away.
    vertical_crossing_cap = 3
    level_gap: dict[int, float] = {}
    for lvl in range(max_level_val + 1):
        vertical_room = min(max(0, crossing_at_level[lvl] - 1), vertical_crossing_cap) * 20
        level_gap[lvl] = level_height[lvl] + y_gap + vertical_room

    # Pay the rest of the room budget in width: each through-edge is a pipe needing a
    # lateral lane (stroke + clearance), so the busiest level sets how much wider the
    # node spacing must be for those pipes to coexist without overlapping.
    lane_width = 14
    x_gap = x_gap + min(peak_crossing, 12) * lane_width

    # Place each node at the vertical center of its row (cumulative_y is the row's top edge).
    node_y: dict[str, float] = {}
    cumulative_y = 30.0
    for lvl in range(max_level_val + 1):
        row_center = cumulative_y + level_height[lvl] / 2
        for name in all_names:
            if level.get(name) == lvl:
                node_y[name] = row_center
        cumulative_y += level_gap.get(lvl, level_height[lvl] + y_gap)

    # Phase 4: Assign X coordinates.
    # First fix a per-level column ORDER that minimizes edge crossings (Sugiyama median sweeps),
    # then turn that order into coordinates below. Ordering is separated from placement so each
    # node's column accounts for both its inputs above and its consumers below, instead of only
    # whichever neighbors happened to be placed in a single top-down pass.
    levels: dict[int, list[str]] = {}
    for name in all_names:
        levels.setdefault(level[name], []).append(name)

    levels = _minimize_crossings(levels, inputs_of, consumers_of, node_y, edge_list)

    node_x: dict[str, float] = {}

    for lvl in sorted(levels.keys()):
        names_at_level = levels[lvl]

        for name in names_at_level:
            deps = inputs_of.get(name, [])
            if len(deps) == 1 and deps[0] in node_x:
                node_x[name] = node_x[deps[0]]
            elif len(deps) > 1:
                dep_xs = [node_x[d] for d in deps if d in node_x]
                if dep_xs:
                    node_x[name] = sum(dep_xs) / len(dep_xs)

        placed = [n for n in names_at_level if n in node_x]
        unplaced = [n for n in names_at_level if n not in node_x]

        if unplaced:
            if placed:
                rightmost = max(node_x[n] + node_size(n)[0] / 2 for n in placed)
                for name in unplaced:
                    w = node_size(name)[0]
                    node_x[name] = rightmost + x_gap + w / 2
                    rightmost = node_x[name] + w / 2
            else:
                cx = 0.0
                for name in unplaced:
                    w = node_size(name)[0]
                    node_x[name] = cx + w / 2
                    cx += w + x_gap

        sorted_names = sorted(names_at_level, key=lambda n: node_x[n])
        for i in range(1, len(sorted_names)):
            prev_name = sorted_names[i - 1]
            curr_name = sorted_names[i]
            prev_right = node_x[prev_name] + node_size(prev_name)[0] / 2
            curr_left = node_x[curr_name] - node_size(curr_name)[0] / 2
            if curr_left < prev_right + x_gap:
                node_x[curr_name] = prev_right + x_gap + node_size(curr_name)[0] / 2

    # Phase 5: Re-center multi-input nodes
    for lvl in sorted(levels.keys(), reverse=True):
        for name in levels[lvl]:
            deps = inputs_of.get(name, [])
            if len(deps) > 1:
                dep_xs = [node_x[d] for d in deps if d in node_x]
                if dep_xs:
                    node_x[name] = sum(dep_xs) / len(dep_xs)

    # Phase 6: Final overlap fix
    for lvl in sorted(levels.keys()):
        sorted_names = sorted(levels[lvl], key=lambda n: node_x[n])
        for i in range(1, len(sorted_names)):
            prev_name = sorted_names[i - 1]
            curr_name = sorted_names[i]
            prev_right = node_x[prev_name] + node_size(prev_name)[0] / 2
            curr_left = node_x[curr_name] - node_size(curr_name)[0] / 2
            if curr_left < prev_right + x_gap:
                node_x[curr_name] = prev_right + x_gap + node_size(curr_name)[0] / 2

    # Shift so leftmost node has padding
    min_x = min(node_x[n] - node_size(n)[0] / 2 for n in all_names)
    if min_x < 30:
        shift = 30 - min_x
        for name in all_names:
            node_x[name] += shift

    # Build LayoutNodes
    layout_nodes: dict[str, LayoutNode] = {}
    for name in all_names:
        w, h = node_size(name)
        cx, cy = node_x[name], node_y[name]

        if name == "__output__":
            ln = LayoutNode(
                name=name,
                kind="output",
                title=output_label,
                description="",
                state="SOURCE",
                is_leaf=False,
                x=cx,
                y=cy,
                width=w,
                height=h,
            )
        elif name in nodes:
            meta = nodes[name]
            is_leaf = name not in targets
            ln = LayoutNode(
                name=name,
                kind=meta.kind,
                title=meta.title,
                description=meta.description,
                state=meta.state,
                is_leaf=is_leaf,
                x=cx,
                y=cy,
                width=w,
                height=h,
                params=list(meta.params),
            )
        else:
            continue
        layout_nodes[name] = ln

    edge_pairs: list[tuple[LayoutNode, LayoutNode]] = []
    for source, target in edges:
        if source in layout_nodes and target in layout_nodes:
            edge_pairs.append((layout_nodes[source], layout_nodes[target]))
    if has_output and "__output__" in layout_nodes:
        assert final_step_name is not None
        if final_step_name in layout_nodes:
            edge_pairs.append((layout_nodes[final_step_name], layout_nodes["__output__"]))

    return list(layout_nodes.values()), edge_pairs


# ── SVG renderer (stdlib only) ───────────────────────────────────────────────


_BoxRect = tuple[float, float, float, float, str]


# ── SVG primitives ───────────────────────────────────────────────────────────


def _svg_rect(
    parent: ET.Element,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    rx: float = 0,
    **attrs: str,
) -> None:
    ET.SubElement(
        parent,
        "rect",
        {
            "x": str(x),
            "y": str(y),
            "width": str(width),
            "height": str(height),
            "rx": str(rx),
            **attrs,
        },
    )


def _svg_ellipse(
    parent: ET.Element, cx: float, cy: float, rx: float, ry: float, **attrs: str
) -> None:
    ET.SubElement(
        parent, "ellipse", {"cx": str(cx), "cy": str(cy), "rx": str(rx), "ry": str(ry), **attrs}
    )


def _svg_text(
    parent: ET.Element,
    text: str,
    x: float,
    y: float,
    *,
    font_size: int = 12,
    font_weight: str = "normal",
    css_class: str = "",
    **attrs: str,
) -> None:
    element = ET.SubElement(
        parent,
        "text",
        {
            "x": str(x),
            "y": str(y),
            "class": f"dag-text {css_class}".strip(),
            "font-family": "Helvetica Neue, sans-serif",
            "font-size": str(font_size),
            "text-anchor": "middle",
            "dominant-baseline": "central",
            "font-weight": font_weight,
            **attrs,
        },
    )
    element.text = text


def _svg_arrowhead(parent: ET.Element, x: float, y: float, css_class: str) -> None:
    """Downward-pointing filled triangle."""
    size = 8
    angle = math.pi / 2
    a1 = angle + math.pi - 0.4
    a2 = angle + math.pi + 0.4
    cx1, cy1 = x + size * math.cos(a1), y + size * math.sin(a1)
    cx2, cy2 = x + size * math.cos(a2), y + size * math.sin(a2)
    points = f"{x:.2f},{y:.2f} {cx1:.2f},{cy1:.2f} {cx2:.2f},{cy2:.2f}"
    ET.SubElement(parent, "polygon", {"points": points, "class": css_class})


def _svg_root(
    canvas_w: float, canvas_h: float, options: SvgOptions = _DEFAULT_SVG_OPTIONS
) -> ET.Element:
    """Create the root <svg>: an intrinsic size plus a viewBox.

    Both matter. The width/height are the scale the layout was computed at, which
    is the size the graph should render at by default; without them a host that
    sets a width silently upscales every label. The viewBox is what lets that host
    scale it down to fit when it needs to (``max-width: 100%; height: auto``).

    The default stylesheet travels inside the document so it still looks right
    opened on its own (a notebook, a browser tab), and can be dropped when the
    host wants to own every colour.
    """
    svg = ET.Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "class": "dag",
            "width": str(canvas_w),
            "height": str(canvas_h),
            "viewBox": f"0 0 {canvas_w} {canvas_h}",
        },
    )
    if options.include_styles:
        style = ET.SubElement(svg, "style")
        style.text = options.stylesheet()
    if options.include_background:
        ET.SubElement(
            svg, "rect", {"class": "dag-canvas", "width": str(canvas_w), "height": str(canvas_h)}
        )
    return svg


# ── Layout helpers ───────────────────────────────────────────────────────────


def _fit_nodes_to_canvas(nodes: list[LayoutNode], padding: float) -> tuple[float, float]:
    """Shift nodes so all fit within padding. Returns (canvas_w, canvas_h)."""
    if not nodes:
        return 100.0, 100.0
    min_left = min(n.x - n.width / 2 for n in nodes)
    min_top = min(n.y - n.height / 2 for n in nodes)
    dx = max(0.0, padding - min_left)
    dy = max(0.0, padding - min_top)
    if dx or dy:
        for node in nodes:
            node.x += dx
            node.y += dy
    canvas_w = max(n.x + n.width / 2 for n in nodes) + padding
    canvas_h = max(n.y + n.height / 2 for n in nodes) + padding
    return canvas_w, canvas_h


def _compute_arrival_x(edges: list[tuple[LayoutNode, LayoutNode]]) -> dict[tuple[str, str], float]:
    """Compute where each edge arrives at its target on the x axis.

    Multiple edges entering the same target are spread evenly across 30% of
    the target width so their entry points don't overlap.
    """
    incoming: dict[str, list[tuple[LayoutNode, LayoutNode]]] = {}
    for src, tgt in edges:
        incoming.setdefault(tgt.name, []).append((src, tgt))

    arrival: dict[tuple[str, str], float] = {}
    for pairs in incoming.values():
        if len(pairs) == 1:
            src, tgt = pairs[0]
            arrival[(src.name, tgt.name)] = tgt.x
        else:
            sorted_pairs = sorted(pairs, key=lambda p: p[0].x)
            tgt = sorted_pairs[0][1]
            usable = tgt.width * 0.3
            count = len(sorted_pairs)
            for i, (src, tgt) in enumerate(sorted_pairs):
                offset = -usable / 2 + (i / max(count - 1, 1)) * usable
                arrival[(src.name, tgt.name)] = tgt.x + offset
    return arrival


def _compute_box_rects(nodes: list[LayoutNode], margin: float) -> list[_BoxRect]:
    """Return each node's bounding box expanded by margin, for overlap detection."""
    return [
        (
            n.x - n.width / 2 - margin,
            n.y - n.height / 2 - margin,
            n.x + n.width / 2 + margin,
            n.y + n.height / 2 + margin,
            n.name,
        )
        for n in nodes
    ]


# ── Quintic edge routing ─────────────────────────────────────────────────────


def _optimize_quintic(
    x1: float,
    y1: float,
    x2: float,
    arrow_base_y: float,
    src_name: str,
    tgt_name: str,
    box_rects: list[_BoxRect],
) -> list[tuple[float, float]]:
    """Find the quintic path from (x1,y1) to (x2,arrow_base_y) that minimizes
    curvature while avoiding intermediate node bounding boxes.

    The quintic x(t) satisfies x(0)=x1, x'(0)=0, x(1)=x2, x'(1)=0 (vertical
    exit and entry). a2 and a3 are free parameters found by grid search +
    refinement.

    The search evaluates ~800 (a2, a3) candidates, and every candidate shares
    the same sample grid (t-powers and y-coordinates) and the same obstacle
    boxes. Those are precomputed ONCE here rather than rebuilt per evaluation,
    and the sampled polyline is materialized only for the winning candidate
    (the search needs the cost, not the points). This optimizer dominates the
    cost of rendering a graph, and the result is numerically identical.
    """
    dx = x2 - x1
    total_dy = arrow_base_y - y1
    n_eval = 100

    # Per-sample constants, independent of (a2, a3): (t², t³, t⁴, t⁵, py).
    samples: list[tuple[float, float, float, float, float]] = []
    for i in range(n_eval + 1):
        t = i / n_eval
        samples.append((t**2, t**3, t**4, t**5, y1 + total_dy * t))
    # A curve never penalizes overlap with its own endpoints' boxes, drop them
    # once instead of re-checking the name per sample per evaluation.
    obstacles = [
        (left, top, right, bottom)
        for left, top, right, bottom, bname in box_rects
        if bname not in (src_name, tgt_name)
    ]

    def cost(a2: float, a3: float, bound: float) -> float:
        """Cost = curvature + box-overlap penalty. Both terms only accumulate,
        so the running total is monotonic, once it reaches ``bound`` (the best
        cost found so far) the candidate cannot win, and we stop early. The
        returned value is then a lower bound ≥ ``bound``, which is all the
        caller's ``< best_cost`` comparison needs.
        """
        a4 = -3 * a2 - 2 * a3 + 5 * dx
        a5 = 2 * a2 + a3 - 4 * dx
        curvature = 0.0
        overlap = 0.0
        prev_px = x1
        for t2, t3, t4, t5, py in samples:
            px = x1 + a2 * t2 + a3 * t3 + a4 * t4 + a5 * t5
            lateral = px - prev_px  # 0 at the first sample, so curvature is unaffected
            curvature += lateral * lateral
            for box_left, box_top, box_right, box_bottom in obstacles:
                if box_left < px < box_right and box_top < py < box_bottom:
                    overlap += 1.0
            running = curvature + 50.0 * overlap * overlap
            if running >= bound:
                return running
            prev_px = px
        return curvature + 50.0 * overlap * overlap

    search_range = max(abs(dx) * 2, 300)
    best_a2, best_a3 = 0.0, 0.0
    best_cost = cost(0.0, 0.0, float("inf"))

    for i2 in range(-10, 11):
        for i3 in range(-10, 11):
            candidate_cost = cost(i2 * search_range / 10, i3 * search_range / 10, best_cost)
            if candidate_cost < best_cost:
                best_cost = candidate_cost
                best_a2 = i2 * search_range / 10
                best_a3 = i3 * search_range / 10

    step = search_range / 10
    for _ in range(3):
        step /= 5
        base_a2, base_a3 = best_a2, best_a3
        for i2 in range(-5, 6):
            for i3 in range(-5, 6):
                candidate_cost = cost(base_a2 + i2 * step, base_a3 + i3 * step, best_cost)
                if candidate_cost < best_cost:
                    best_cost = candidate_cost
                    best_a2 = base_a2 + i2 * step
                    best_a3 = base_a3 + i3 * step

    # Materialize the polyline only for the winning parameters.
    a4 = -3 * best_a2 - 2 * best_a3 + 5 * dx
    a5 = 2 * best_a2 + best_a3 - 4 * dx
    return [
        (x1 + best_a2 * t2 + best_a3 * t3 + a4 * t4 + a5 * t5, py) for t2, t3, t4, t5, py in samples
    ]


def _edge_tail_parts(
    points: list[tuple[float, float]], x2: float, arrow_base_y: float
) -> list[str]:
    """Build the SVG path commands for an edge tail: the full routed quintic, straight to the arrow base.

    The quintic is constructed with a vertical entry tangent (``x'(1) = 0``), so it already arrives at
    ``(x2, arrow_base_y)`` perpendicular to the target box. Drawing the whole curve keeps that tangent, so
    the join is continuous by construction. (The earlier truncate-and-stub approach discarded the curve's
    own vertical final approach and replaced it with a hard corner, which is what kinked the line.)
    """
    parts = [f"M {points[0][0]:.1f},{points[0][1]:.1f}"]
    for px, py in points[1:]:
        parts.append(f"L {px:.1f},{py:.1f}")
    # The quintic ends at (x2, arrow_base_y); guard against float drift so the
    # arrowhead sits exactly on the path's end.
    if abs(points[-1][0] - x2) > 0.05 or abs(points[-1][1] - arrow_base_y) > 0.05:
        parts.append(f"L {x2:.1f},{arrow_base_y:.1f}")
    return parts


def _draw_quintic_edge(
    parent: ET.Element,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    src_name: str,
    tgt_name: str,
    box_rects: list[_BoxRect],
    dashed: bool,
) -> None:
    """Draw an edge with quintic routing (straight line when src and tgt are aligned).

    ``dashed`` marks the edge into the output node, which is a hand-off rather
    than a data dependency.
    """
    arrow_size = 8
    arrow_base_y = y2 - arrow_size
    edge_class = "dag-edge dag-edge--output" if dashed else "dag-edge"

    if abs(x1 - x2) < 5:
        attrs: dict[str, str] = {
            "x1": f"{x1:.1f}",
            "y1": f"{y1:.1f}",
            "x2": f"{x2:.1f}",
            "y2": f"{arrow_base_y:.1f}",
            "class": edge_class,
            "stroke-width": "1.5",
        }
        if dashed:
            attrs["stroke-dasharray"] = "6,4"
        ET.SubElement(parent, "line", attrs)
    else:
        best_points = _optimize_quintic(x1, y1, x2, arrow_base_y, src_name, tgt_name, box_rects)
        parts = _edge_tail_parts(best_points, x2, arrow_base_y)
        path_attrs: dict[str, str] = {
            "d": " ".join(parts),
            "class": edge_class,
            "stroke-width": "1.5",
            "stroke-linejoin": "round",
            "stroke-linecap": "round",
        }
        if dashed:
            path_attrs["stroke-dasharray"] = "6,4"
        ET.SubElement(parent, "path", path_attrs)

    _svg_arrowhead(parent, x2, y2, "dag-arrow dag-arrow--output" if dashed else "dag-arrow")


# ── Node renderers ───────────────────────────────────────────────────────────


def _svg_param_node(parent: ET.Element, node: LayoutNode) -> None:
    _svg_ellipse(
        parent,
        node.x,
        node.y,
        node.width / 2,
        node.height / 2,
        **{"class": "dag-node dag-node--param", "stroke-width": "1.5"},
    )
    _svg_text(parent, node.title, node.x, node.y, font_size=12, css_class="dag-text--param-node")


def _svg_file_node(parent: ET.Element, node: LayoutNode) -> None:
    left, top = node.x - node.width / 2, node.y - node.height / 2
    _svg_rect(
        parent,
        left,
        top,
        node.width,
        node.height,
        rx=4,
        **{"class": "dag-node dag-node--file", "stroke-width": "1.5"},
    )
    _svg_text(parent, node.title, node.x, node.y, font_size=11, css_class="dag-text--file")


def _svg_output_node(parent: ET.Element, node: LayoutNode) -> None:
    left, top = node.x - node.width / 2, node.y - node.height / 2
    _svg_rect(
        parent,
        left,
        top,
        node.width,
        node.height,
        rx=8,
        **{"class": "dag-node dag-node--output", "stroke-width": "2"},
    )
    _svg_text(
        parent,
        node.title,
        node.x,
        node.y,
        font_size=12,
        font_weight="bold",
        css_class="dag-text--output",
    )


def _svg_step_node(parent: ET.Element, node: LayoutNode) -> None:
    """Render a step box with title, description, and scalar parameters."""
    kind_class = "dag-node--leaf" if node.is_leaf else "dag-node--step"
    state_class = f"is-{node.state.lower()}"
    lw = "2" if node.is_leaf else "1.5"

    left, top = node.x - node.width / 2, node.y - node.height / 2
    _svg_rect(
        parent,
        left,
        top,
        node.width,
        node.height,
        rx=10,
        **{"class": f"dag-node {kind_class} {state_class}", "stroke-width": lw},
    )

    desc_lines = (
        _wrap_text_lines(node.description, int((node.width - 20) / 6.5)) if node.description else []
    )
    line_height = 14
    param_line_height = 13
    desc_block_h = len(desc_lines) * line_height
    param_block_h = len(node.params) * param_line_height
    total_content_h = (
        14 + (6 + desc_block_h if desc_lines else 0) + (6 + param_block_h if node.params else 0)
    )

    title_y = node.y - total_content_h / 2 + 7
    _svg_text(parent, node.title, node.x, title_y, font_size=14, font_weight="bold")

    cursor_y = title_y + 7
    if desc_lines:
        cursor_y += 6
        for i, line in enumerate(desc_lines):
            _svg_text(
                parent,
                line,
                node.x,
                cursor_y + i * line_height + line_height / 2,
                font_size=10,
                css_class="dag-text--desc",
            )
        cursor_y += desc_block_h

    if node.params:
        cursor_y += 6
        mono = {"font-family": "monospace"}
        for i, (name, value) in enumerate(node.params):
            y = cursor_y + i * param_line_height + param_line_height / 2
            _svg_text(
                parent,
                f"{name} = {value}",
                node.x,
                y,
                font_size=9,
                css_class="dag-text--param",
                **mono,
            )


def _svg_node(parent: ET.Element, node: LayoutNode) -> None:
    """Dispatch to the appropriate node renderer by kind."""
    renderers = {"param": _svg_param_node, "file": _svg_file_node, "output": _svg_output_node}
    renderers.get(node.kind, _svg_step_node)(parent, node)


# ── Main SVG renderer ────────────────────────────────────────────────────────


def _render_layout(
    layout_nodes: list[LayoutNode],
    edge_pairs: list[tuple[LayoutNode, LayoutNode]],
    options: SvgOptions = _DEFAULT_SVG_OPTIONS,
) -> str:
    """Render positioned LayoutNodes + edge pairs as an SVG string."""
    canvas_w, canvas_h = _fit_nodes_to_canvas(layout_nodes, padding=30)
    arrival_x = _compute_arrival_x(edge_pairs)
    box_rects = _compute_box_rects(layout_nodes, margin=12)

    svg = _svg_root(canvas_w, canvas_h, options)

    for src, tgt in edge_pairs:
        is_output = tgt.kind == "output"
        ax = arrival_x.get((src.name, tgt.name), tgt.x)
        x1, y1 = src.x, src.y + src.height / 2
        y2 = tgt.y - tgt.height / 2
        _draw_quintic_edge(svg, x1, y1, ax, y2, src.name, tgt.name, box_rects, is_output)

    for node in layout_nodes:
        _svg_node(svg, node)

    return ET.tostring(svg, encoding="unicode")


def _walk_to_metadata(
    graph_nodes: list[Node], *, cache_dir: Path | None, prefix: str
) -> dict[str, NodeMetadata]:
    """Convert walk results into NodeMetadata, doing Step introspection up front."""
    result: dict[str, NodeMetadata] = {}
    for node in graph_nodes:
        if node.kind == "step" and node.step is not None:
            title = node.title or _camel_to_words(
                node.name.removeprefix(prefix) if prefix else node.name
            )
            description = _first_doc_line(node.step)
            params = tuple(_scalar_params(node.step))
        else:
            title = node.name
            description = ""
            params = ()
        result[node.name] = NodeMetadata(
            name=node.name,
            kind=node.kind,
            title=title,
            description=description,
            params=params,
            state=compute_state(node, cache_dir),
        )
    return result


def node_metadata(
    nodes: list[Node], *, cache_dir: Path | None = None, prefix: str = ""
) -> list[NodeMetadata]:
    """Return the display data for each node from ``walk``, in the same order.

    Use it to draw a pipeline your own way: walk the graph, change the ``NodeMetadata`` you want
    (``dataclasses.replace`` a title, a description or the params), then pass the list to
    ``render_graph_svg``. ``dag`` does the same with no changes.

    Args:
        nodes: the nodes returned by ``walk``.
        cache_dir: colour each step by its cache state, as ``dag(..., cache_dir=)`` does.
        prefix: strip this from step names in the titles.
    """
    by_name = _walk_to_metadata(nodes, cache_dir=cache_dir, prefix=prefix)
    return [by_name[node.name] for node in nodes]


def _render_svg(
    output: Deferred[Any],
    graph_nodes: list[Node],
    graph_edges: list[Edge],
    *,
    cache_dir: Path | None = None,
    prefix: str = "",
    path: str | Path | None = None,
    options: SvgOptions = _DEFAULT_SVG_OPTIONS,
) -> str:
    """Render the DAG (walked from a Deferred output) as an SVG string."""
    nodes = _walk_to_metadata(graph_nodes, cache_dir=cache_dir, prefix=prefix)
    edges = [(e.source, e.target) for e in graph_edges]

    output_label = ""
    final_step_name: str | None = None
    if isinstance(output, StepResult):
        result_type = output.step.result_model()
        output_label = _camel_to_words(result_type.__name__) if result_type else "output"
        final_step_name = type(output.step).__name__

    if not options.show_params:
        nodes = {name: replace(node, params=()) for name, node in nodes.items()}
    layout_nodes, edge_pairs = _compute_layout(
        nodes, edges, output_label=output_label, final_step_name=final_step_name
    )
    svg_string = _render_layout(layout_nodes, edge_pairs, options)

    if path is not None:
        atomic_write_text(Path(str(path)), svg_string)
        logger.info("dag.svg.saved", path=str(path))

    return svg_string


def render_graph_svg(
    nodes: list[NodeMetadata],
    edges: list[tuple[str, str]],
    *,
    output_label: str = "",
    final_step_name: str | None = None,
    options: SvgOptions = _DEFAULT_SVG_OPTIONS,
) -> str:
    """Return the SVG for explicit node metadata and ``(source, target)`` edge pairs.

    Use it to draw a graph that is not a live pipeline, for example one built from your own
    records. ``dag(output)`` and ``render_pipeline_svg(pipeline_dir)`` call the same layout.
    """
    # Strip params before layout, not at paint time: the box sizes are computed
    # from the same metadata, so dropping them later would leave empty space.
    if not options.show_params:
        nodes = [replace(n, params=()) for n in nodes]
    nodes_dict = {n.name: n for n in nodes}
    layout_nodes, edge_pairs = _compute_layout(
        nodes_dict, edges, output_label=output_label, final_step_name=final_step_name
    )
    return _render_layout(layout_nodes, edge_pairs, options)


_render_graph_svg = render_graph_svg  # the first-release private name, kept for old callers


def _humanize_step_name(name: str) -> str:
    """Strip a numeric ordering prefix and convert snake_case into title-cased words."""
    cleaned = re.sub(r"^\d+_", "", name)
    return cleaned.replace("_", " ").title()


def _scalar_params_from_spec(spec: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Extract scalar entries from a step spec dict for display in the SVG."""
    items: list[tuple[str, str]] = []
    for key, value in spec.items():
        if isinstance(value, bool):
            items.append((key, str(value)))
        elif isinstance(value, (int, float)):
            items.append((key, f"{value:g}" if isinstance(value, float) else str(value)))
        elif isinstance(value, str) and 0 < len(value) <= 30:
            items.append((key, value))
        elif value is None:
            items.append((key, "None"))
    return tuple(items)


def render_pipeline_svg(
    pipeline_dir: str | Path, *, options: SvgOptions = _DEFAULT_SVG_OPTIONS
) -> str | None:
    """Render the pipeline at ``pipeline_dir`` as SVG from its on-disk manifest.

    Reads ``pipeline.json`` and each step's ``step.json``, builds the graph,
    and produces an SVG string. Returns ``None`` when there is no manifest
    or the manifest declares no steps.
    """
    base = str(pipeline_dir)
    pipeline_path = os.path.join(base, "pipeline.json")
    if not os.path.exists(pipeline_path):
        return None
    with open(pipeline_path) as f:
        pipeline_data = json.load(f)

    step_dirs: list[str] = pipeline_data.get("steps", [])
    if not step_dirs:
        return None

    nodes: list[NodeMetadata] = []
    for step_dir in step_dirs:
        step_json_path = os.path.join(base, step_dir, "step.json")
        spec: dict[str, Any] = {}
        kind = ""
        if os.path.exists(step_json_path):
            with open(step_json_path) as f:
                step_data = json.load(f)
            spec = step_data.get("spec", {})
            kind = step_data.get("kind", "")

        nodes.append(
            NodeMetadata(
                name=step_dir,
                kind="step",
                title=_humanize_step_name(step_dir),
                description=kind,
                params=_scalar_params_from_spec(spec),
            )
        )

    edges: list[tuple[str, str]] = []
    for edge in pipeline_data.get("edges", []):
        source = edge.get("source", "")
        target = edge.get("target", "")
        if source.startswith("parameter:"):
            continue
        src_node = source.split(".", 1)[0]
        tgt_node = target.split(".", 1)[0]
        if src_node and tgt_node:
            edges.append((src_node, tgt_node))

    final_step_name = pipeline_data.get("output") or step_dirs[-1]
    return render_graph_svg(
        nodes, edges, output_label="", final_step_name=final_step_name, options=options
    )
