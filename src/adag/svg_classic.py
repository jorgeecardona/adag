"""The layout and edge drawing of adag 0.3.22, kept for ``style="classic"``.

The levels, the column order, the x placement and the quintic edge curves are the code of release
0.3.22 (commit 2811cba), unchanged, so a graph drawn with ``classic`` has the same boxes and the
same edge paths as before. Only the boxes' painting lives elsewhere (``svg_paint``). The curve
search is slow (about a second for 12 nodes). That is the old behaviour; the new styles use
``svg_layout`` and ``svg_route`` instead.
"""
# ruff: noqa: PLR2004, S101, FBT001  (the old code is kept as it was, so a diff against 0.3.22 stays small)

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from adag import svg_layout as lay

if TYPE_CHECKING:
    from adag.svg_paint import Measured, RNode

_BoxRect = tuple[float, float, float, float, str]


@dataclass
class LayoutNode:
    """A positioned node."""

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
    shape: str = ""


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


def compute_layout(
    nodes: dict[str, RNode],
    sizes: Mapping[str, tuple[float, float]],
    edges: list[tuple[str, str]],
    *,
    output_label: str = "",
    final_step_name: str | None = None,
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
        return sizes[name]

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
                shape=meta.shape,
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


@dataclass(frozen=True)
class ClassicDrawing:
    """Where the old layout put the nodes, and the edges to draw between them."""

    nodes: dict[str, lay.Placed]
    width: float
    height: float
    edges: tuple[tuple[LayoutNode, LayoutNode], ...]
    boxes: tuple[_BoxRect, ...]
    arrival: dict[tuple[str, str], float]


def place(
    nodes: Sequence[RNode],
    edges: Sequence[tuple[str, str]],
    measured: Mapping[str, Measured],
    *,
    output_label: str,
    output: lay.LNode | None,
    final: str | None,
) -> ClassicDrawing:
    """Lay out the graph the way 0.3.22 did, from the sizes the painter measured."""
    known = {n.name: n for n in nodes}
    sizes = {name: (m.width, m.height) for name, m in measured.items()}
    if output is not None:
        sizes[lay.OUTPUT] = (output.width, output.height)
    pairs = [(a, b) for a, b in edges if a in known and b in known]
    has_output = output is not None and final in known
    layout_nodes, edge_pairs = compute_layout(
        known,
        sizes,
        pairs,
        output_label=output_label if has_output else "",
        final_step_name=final if has_output else None,
    )
    width, height = _fit_nodes_to_canvas(layout_nodes, padding=30)
    placed = {n.name: lay.Placed(n.name, n.x, n.y, n.width, n.height, 0) for n in layout_nodes}
    return ClassicDrawing(
        placed,
        width,
        height,
        tuple(edge_pairs),
        tuple(_compute_box_rects(layout_nodes, margin=12)),
        _compute_arrival_x(edge_pairs),
    )


def paint_edges(parent: ET.Element, drawing: ClassicDrawing) -> None:
    """Draw every edge with the quintic curves of 0.3.22."""
    for src, tgt in drawing.edges:
        ax = drawing.arrival.get((src.name, tgt.name), tgt.x)
        _draw_quintic_edge(
            parent,
            src.x,
            src.y + src.height / 2,
            ax,
            tgt.y - tgt.height / 2,
            src.name,
            tgt.name,
            list(drawing.boxes),
            tgt.kind == "output",
        )
