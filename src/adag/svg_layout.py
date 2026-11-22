"""Layered layout for the graph SVG: levels, crossing-free-as-possible order, x placement, routes.

The layout knows box sizes and edges, not how a box is drawn. Long edges get one virtual node per
level they cross, so they keep a lane between the boxes instead of running under them. Each box
is placed at the median of its neighbours above and below, within the room its row allows.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

OUTPUT = "__output__"
_EPS = 0.5

Point = tuple[float, float]


@dataclass(frozen=True)
class LNode:
    """Size of one node, as the painter will draw it."""

    name: str
    width: float
    height: float
    left_extra: float = 0.0
    """Room the painter needs to the left of the box (a compact input label)."""
    top_extra: float = 0.0
    """Room the painter needs above the box (the tabs row)."""
    arrive_lo: float = 0.0
    """Where edges may arrive on the top edge, as an offset from the box centre."""
    arrive_hi: float = 0.0
    kind: str = "step"


@dataclass(frozen=True)
class LayoutSettings:
    """Distances of the layout, in px."""

    margin: float = 30.0
    gap_x: float = 36.0
    lane_x: float = 22.0
    gap_y: float = 64.0
    slot: float = 9.0
    arrow: float = 9.0
    corner: float = 14.0
    port_margin: float = 0.2
    min_port_spacing: float = 14.0


@dataclass(frozen=True)
class Placed:
    """A box with its centre and its level."""

    name: str
    x: float
    y: float
    width: float
    height: float
    level: int
    left_extra: float = 0.0
    top_extra: float = 0.0


@dataclass(frozen=True)
class Route:
    """An edge: the points it passes (start at the source, end at the target's top edge).

    ``jogs`` has one y per consecutive pair of points: where a right-angle edge moves sideways.
    """

    source: str
    target: str
    points: tuple[Point, ...]
    jogs: tuple[float, ...]
    dashed: bool = False
    rows: tuple[tuple[float, float] | None, ...] = ()
    """For each point: the top and bottom of the row it crosses (a virtual point), else ``None``."""


@dataclass(frozen=True)
class Layout:
    nodes: dict[str, Placed]
    routes: list[Route]
    width: float
    height: float
    rows: dict[int, tuple[float, float]] = field(default_factory=dict[int, tuple[float, float]])


# ── Levels ───────────────────────────────────────────────────────────────────


def _levels(
    names: Sequence[str],
    inputs_of: Mapping[str, Sequence[str]],
    consumers_of: Mapping[str, Sequence[str]],
) -> dict[str, int]:
    level: dict[str, int] = {}

    def assign(name: str) -> int:
        if name in level:
            return level[name]
        level[name] = 0  # a cycle would otherwise recurse forever
        deps = inputs_of.get(name, ())
        level[name] = max((assign(d) for d in deps), default=-1) + 1
        return level[name]

    for name in names:
        assign(name)
    # A node with no inputs sits just above its first consumer, not at the top.
    for name in names:
        if not inputs_of.get(name) and consumers_of.get(name):
            level[name] = min(level[c] for c in consumers_of[name]) - 1
    low = min(level.values(), default=0)
    return {name: lvl - low for name, lvl in level.items()}


# ── Order within a level ─────────────────────────────────────────────────────


def _crossings(upper: Sequence[str], lower: Sequence[str], links: Sequence[tuple[str, str]]) -> int:
    upper_pos = {n: i for i, n in enumerate(upper)}
    lower_pos = {n: i for i, n in enumerate(lower)}
    pairs = [(upper_pos[a], lower_pos[b]) for a, b in links if a in upper_pos and b in lower_pos]
    count = 0
    for i, (a1, b1) in enumerate(pairs):
        for a2, b2 in pairs[i + 1 :]:
            if (a1 - a2) * (b1 - b2) < 0:
                count += 1
    return count


def _total_crossings(order: Mapping[int, Sequence[str]], links: Sequence[tuple[str, str]]) -> int:
    levels = sorted(order)
    return sum(_crossings(order[a], order[b], links) for a, b in itertools.pairwise(levels))


def _order_levels(
    order: dict[int, list[str]],
    parents: Mapping[str, Sequence[str]],
    children: Mapping[str, Sequence[str]],
    links: Sequence[tuple[str, str]],
    rounds: int = 10,
) -> dict[int, list[str]]:
    """Barycentre sweeps down and up. Returns the order with the fewest crossings seen."""
    levels = sorted(order)
    best = {lvl: list(names) for lvl, names in order.items()}
    best_count = _total_crossings(best, links)
    current = {lvl: list(names) for lvl, names in order.items()}

    def sweep(lvl: int, neighbours: Mapping[str, Sequence[str]], adjacent: int) -> None:
        position = {n: i for i, n in enumerate(current[adjacent])}
        names = current[lvl]

        def key(item: tuple[int, str]) -> tuple[float, int]:
            index, name = item
            around = [position[n] for n in neighbours.get(name, ()) if n in position]
            return (sum(around) / len(around) if around else float(index), index)

        current[lvl] = [name for _, name in sorted(enumerate(names), key=key)]

    for _ in range(rounds):
        for lvl in levels[1:]:
            sweep(lvl, parents, lvl - 1)
        for lvl in reversed(levels[:-1]):
            sweep(lvl, children, lvl + 1)
        count = _total_crossings(current, links)
        if count < best_count:
            best, best_count = {lvl: list(n) for lvl, n in current.items()}, count
        if best_count == 0:
            break
    return best


# ── x placement ──────────────────────────────────────────────────────────────


def _project(
    desired: Sequence[float], weights: Sequence[float], gaps: Sequence[float]
) -> list[float]:
    """The positions closest to ``desired`` (weighted least squares) that keep ``gaps`` between neighbours.

    Pool adjacent violators on the positions shifted by the running sum of the gaps.
    """
    shift = [0.0]
    for gap in gaps:
        shift.append(shift[-1] + gap)
    blocks: list[list[float]] = []  # each: [weighted sum of targets, weight, count]
    for d, w, s in zip(desired, weights, shift, strict=False):
        blocks.append([w * (d - s), w, 1.0])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            last = blocks.pop()
            blocks[-1] = [blocks[-1][0] + last[0], blocks[-1][1] + last[1], blocks[-1][2] + last[2]]
    result: list[float] = []
    for total, weight, count in blocks:
        result.extend([total / weight] * int(count))
    return [y + s for y, s in zip(result, shift, strict=False)]


def _place_x(
    order: Mapping[int, Sequence[str]],
    left: Mapping[str, float],
    right: Mapping[str, float],
    virtual: set[str],
    parents: Mapping[str, Sequence[str]],
    children: Mapping[str, Sequence[str]],
    settings: LayoutSettings,
) -> dict[str, float]:
    levels = sorted(order)

    def gaps_for(names: Sequence[str]) -> list[float]:
        out: list[float] = []
        for a, b in itertools.pairwise(names):
            gap = settings.lane_x if (a in virtual or b in virtual) else settings.gap_x
            out.append(right[a] + left[b] + gap)
        return out

    x: dict[str, float] = {}
    for lvl in levels:
        names = order[lvl]
        start = _project([0.0] * len(names), [1.0] * len(names), gaps_for(names))
        x.update(zip(names, start, strict=False))

    def pass_over(lvl: int, neighbours: Mapping[str, Sequence[str]], *, both: bool) -> None:
        names = order[lvl]
        desired: list[float] = []
        weights: list[float] = []
        for name in names:
            around = [x[n] for n in neighbours.get(name, ()) if n in x]
            if both:
                around += [x[n] for n in children.get(name, ()) if n in x]
            if around:
                desired.append(sum(around) / len(around))
                weights.append(4.0 if name in virtual else 1.0)
            else:
                desired.append(x[name])
                weights.append(0.1)
        x.update(zip(names, _project(desired, weights, gaps_for(names)), strict=False))

    for _ in range(3):
        for lvl in levels[1:]:
            pass_over(lvl, parents, both=False)
        for lvl in reversed(levels[:-1]):
            pass_over(lvl, children, both=False)
    for lvl in levels:
        pass_over(lvl, parents, both=True)
    return x


# ── Routes ───────────────────────────────────────────────────────────────────


def _spread(count: int, lo: float, hi: float, spacing: float) -> list[float]:
    """``count`` positions inside ``[lo, hi]``, evenly, at least ``spacing`` apart when they fit."""
    if count == 1:
        return [(lo + hi) / 2]
    span = min(hi - lo, spacing * (count - 1)) if count * spacing <= (hi - lo) else hi - lo
    start = (lo + hi) / 2 - span / 2
    return [start + span * i / (count - 1) for i in range(count)]


def layout(
    nodes: Sequence[LNode],
    edges: Sequence[tuple[str, str]],
    *,
    output: LNode | None = None,
    final: str | None = None,
    settings: LayoutSettings | None = None,
) -> Layout:
    """Place ``nodes`` and route ``edges``. ``output`` is a node below ``final``, joined by a dashed edge."""
    settings = settings or LayoutSettings()
    sizes: dict[str, LNode] = {n.name: n for n in nodes}
    names = [n.name for n in nodes]
    edge_list = list(
        dict.fromkeys((a, b) for a, b in edges if a in sizes and b in sizes and a != b)
    )
    dashed: set[tuple[str, str]] = set()
    if output is not None and final is not None and final in sizes:
        sizes[OUTPUT] = output
        names.append(OUTPUT)
        edge_list.append((final, OUTPUT))
        dashed.add((final, OUTPUT))

    inputs_of: dict[str, list[str]] = {n: [] for n in names}
    consumers_of: dict[str, list[str]] = {n: [] for n in names}
    for a, b in edge_list:
        inputs_of[b].append(a)
        consumers_of[a].append(b)
    level = _levels(names, inputs_of, consumers_of)

    # A virtual node on every level an edge crosses.
    virtual: set[str] = set()
    chains: dict[tuple[str, str], list[str]] = {}
    parents: dict[str, list[str]] = {n: [] for n in names}
    children: dict[str, list[str]] = {n: [] for n in names}
    links: list[tuple[str, str]] = []
    for a, b in edge_list:
        chain = [a]
        for lvl in range(level[a] + 1, level[b]):
            v = f"__v{len(virtual)}"
            virtual.add(v)
            level[v] = lvl
            parents[v] = []
            children[v] = []
            chain.append(v)
        chain.append(b)
        chains[(a, b)] = chain
        for u, w in itertools.pairwise(chain):
            parents[w].append(u)
            children[u].append(w)
            links.append((u, w))

    order: dict[int, list[str]] = {}
    for name in [*names, *sorted(virtual)]:
        order.setdefault(level[name], []).append(name)
    order = _order_levels(order, parents, children, links)

    left = {n: (sizes[n].width / 2 + sizes[n].left_extra if n in sizes else 0.0) for n in level}
    right = {n: (sizes[n].width / 2 if n in sizes else 0.0) for n in level}
    x = _place_x(order, left, right, virtual, parents, children, settings)

    # Sideways jogs between two rows share a slot (a y) when their spans do not overlap.
    slot_of: dict[tuple[tuple[str, str], int], int] = {}
    slot_count: dict[int, int] = {}
    pending: dict[int, list[tuple[tuple[str, str], int, float, float]]] = {}
    for key, chain in chains.items():
        for i, (u, w) in enumerate(itertools.pairwise(chain)):
            if abs(x[u] - x[w]) > _EPS:
                pending.setdefault(level[u], []).append((key, i, x[u], x[w]))
    for lvl, segs in pending.items():
        # The jog that ends furthest right goes highest, so the verticals of the others cross less.
        ordered = sorted((s for s in segs if s[3] > s[2]), key=lambda s: -s[3])
        ordered += sorted((s for s in segs if s[3] < s[2]), key=lambda s: s[3])
        slots: list[list[tuple[float, float]]] = []
        for key, i, sx, tx in ordered:
            span = (min(sx, tx) - 6, max(sx, tx) + 6)
            for index, taken in enumerate(slots):
                if all(span[1] < lo or span[0] > hi for lo, hi in taken):
                    taken.append(span)
                    slot_of[(key, i)] = index
                    break
            else:
                slots.append([span])
                slot_of[(key, i)] = len(slots) - 1
        slot_count[lvl] = len(slots)

    # Row heights, then the gap under each row: it grows with the slots it must hold.
    row_height: dict[int, float] = {}
    for lvl, row in order.items():
        real = [sizes[n].height + sizes[n].top_extra for n in row if n in sizes]
        row_height[lvl] = max(real) if real else 0.0
    levels = sorted(order)
    gap_after = {
        lvl: max(
            settings.gap_y,
            4 * settings.slot + settings.arrow + max(0, slot_count.get(lvl, 0) - 1) * settings.slot,
        )
        for lvl in levels
    }
    row_top: dict[int, float] = {}
    cursor = 0.0
    for lvl in levels:
        row_top[lvl] = cursor
        cursor += row_height[lvl] + gap_after[lvl]

    y_center: dict[str, float] = {}
    top_of_box: dict[str, float] = {}
    for lvl, row in order.items():
        mid = row_top[lvl] + row_height[lvl] / 2
        for n in row:
            if n in virtual:
                y_center[n] = mid
            else:
                node = sizes[n]
                full = node.height + node.top_extra
                top = row_top[lvl] + (row_height[lvl] - full) / 2 + node.top_extra
                top_of_box[n] = top
                y_center[n] = top + node.height / 2

    # Ports: departures spread along the source's bottom edge, arrivals along the target's top edge.
    out_edges: dict[str, list[tuple[str, str]]] = {}
    in_edges: dict[str, list[tuple[str, str]]] = {}
    for key, chain in chains.items():
        out_edges.setdefault(chain[0], []).append(key)
        in_edges.setdefault(chain[-1], []).append(key)
    depart: dict[tuple[str, str], float] = {}
    arrive: dict[tuple[str, str], float] = {}
    for src, group in out_edges.items():
        group.sort(key=lambda k: x[chains[k][1]])
        half = sizes[src].width / 2
        lo, hi = (
            x[src] - half * (1 - 2 * settings.port_margin),
            x[src] + half * (1 - 2 * settings.port_margin),
        )
        ports = _spread(len(group), lo, hi, settings.min_port_spacing)
        depart.update(zip(group, ports, strict=False))
    for dst, group in in_edges.items():
        group.sort(key=lambda k: x[chains[k][-2]])
        node = sizes[dst]
        lo = x[dst] + (
            node.arrive_lo
            if node.arrive_lo or node.arrive_hi
            else -node.width * (0.5 - settings.port_margin)
        )
        hi = x[dst] + (
            node.arrive_hi
            if node.arrive_lo or node.arrive_hi
            else node.width * (0.5 - settings.port_margin)
        )
        ports = _spread(len(group), lo, hi, settings.min_port_spacing)
        arrive.update(zip(group, ports, strict=False))

    # Raw point lists, then jog slots per gap.
    raw: dict[tuple[str, str], list[Point]] = {}
    for key, chain in chains.items():
        src, dst = key
        pts: list[Point] = [(depart[key], top_of_box[src] + sizes[src].height)]
        pts.extend((x[v], y_center[v]) for v in chain[1:-1])
        pts.append((arrive[key], top_of_box[dst]))
        raw[key] = pts

    jog_y: dict[tuple[tuple[str, str], int], float] = {}
    for lvl, count in slot_count.items():
        bottom = row_top[lvl] + row_height[lvl]
        lo = bottom + settings.slot * 2
        hi = bottom + gap_after[lvl] - settings.slot * 2 - settings.arrow
        positions = _spread(count, lo, max(lo, hi), settings.slot)
        for (key, i), index in slot_of.items():
            if level[chains[key][i]] == lvl:
                jog_y[(key, i)] = positions[index]

    routes: list[Route] = []
    for key, chain in chains.items():
        pts = raw[key]
        jogs = tuple(
            jog_y.get((key, i), (pts[i][1] + pts[i + 1][1]) / 2) for i in range(len(pts) - 1)
        )
        rows_of: list[tuple[float, float] | None] = [None]
        rows_of.extend(
            (row_top[level[v]], row_top[level[v]] + row_height[level[v]]) for v in chain[1:-1]
        )
        rows_of.append(None)
        routes.append(Route(key[0], key[1], tuple(pts), jogs, key in dashed, tuple(rows_of)))

    # Canvas.
    placed: dict[str, Placed] = {}
    for n in names:
        node = sizes[n]
        placed[n] = Placed(
            n, x[n], y_center[n], node.width, node.height, level[n], node.left_extra, node.top_extra
        )
    min_x = min((p.x - p.width / 2 - p.left_extra for p in placed.values()), default=0.0)
    min_y = min((p.y - p.height / 2 - p.top_extra for p in placed.values()), default=0.0)
    for r in routes:
        for px, _py in r.points:
            min_x = min(min_x, px)
    dx, dy = settings.margin - min_x, settings.margin - min_y
    shifted = {
        n: Placed(p.name, p.x + dx, p.y + dy, p.width, p.height, p.level, p.left_extra, p.top_extra)
        for n, p in placed.items()
    }
    out_routes = [
        Route(
            r.source,
            r.target,
            tuple((px + dx, py + dy) for px, py in r.points),
            tuple(j + dy for j in r.jogs),
            r.dashed,
            tuple(None if row is None else (row[0] + dy, row[1] + dy) for row in r.rows),
        )
        for r in routes
    ]
    max_x = max((p.x + p.width / 2 for p in shifted.values()), default=0.0)
    max_y = max((p.y + p.height / 2 for p in shifted.values()), default=0.0)
    for r in out_routes:
        for px, _ in r.points:
            max_x = max(max_x, px)
    rows = {lvl: (row_top[lvl] + dy, row_top[lvl] + row_height[lvl] + dy) for lvl in levels}
    return Layout(
        shifted,
        out_routes,
        math.ceil(max_x + settings.margin),
        math.ceil(max_y + settings.margin),
        rows,
    )
