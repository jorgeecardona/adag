"""Layered layout for the graph SVG: levels, crossing-free-as-possible order, x placement, routes.

The layout knows box sizes and edges, not how a box is drawn. Long edges get one virtual node per
level they cross, so they keep a lane between the boxes instead of running under them. Each box
is placed at the median of its neighbours above and below, within the room its row allows.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from adag.svg_route import (
    BOX_MARGIN,
    Jog,
    Plan,
    Request,
    RowBox,
    SideRequest,
    Taken,
    assign_slots,
    plan_first,
    plan_rest,
)

OUTPUT = "__output__"
_EPS = 0.5
SNAP_SPACING = 12.0
_PORT_AND_ARRIVAL = 2  # a plan with no row between the two levels has just these two columns
KINK = 14.0

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
    arrive_at: tuple[tuple[str, float], ...] = ()
    """``(source name, x offset from the box centre)``: the edge from that source ends exactly
    there, on the top edge. The node takes no edge on a side face. A frame uses it to land each
    edge on an input inside the frame."""
    depart_at: float | None = None
    """The x offset from the box centre where every edge leaves this node (a frame's exit)."""


@dataclass(frozen=True)
class LayoutSettings:
    """Distances of the layout, in px."""

    margin: float = 30.0
    gap_x: float = 36.0
    lane_x: float = 22.0
    gap_y: float = 64.0
    slot: float = 12.0
    arrow: float = 9.0
    port_margin: float = 0.2
    corner_room: float = 12.0
    min_port_spacing: float = 14.0
    side_entry: bool = True
    """False: every edge ends on the top of its target (the band style has only curves)."""


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
    """An edge: the corners of its path, from the bottom of the source to the top of the target.

    The path is made of vertical and horizontal runs. A horizontal run is a sideways move, and it
    lies in the gap between two rows.
    """

    source: str
    target: str
    points: tuple[Point, ...]
    dashed: bool = False


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
    top: frozenset[str] = frozenset(),
) -> dict[str, int]:
    """Level of each node. Nodes in ``top`` are level 0 and every other node is below them."""
    level: dict[str, int] = {}
    # The other nodes are leveled as if the top row were not there.
    parents = {n: [p for p in inputs_of.get(n, ()) if p not in top] for n in names}
    children = {n: [c for c in consumers_of.get(n, ()) if c not in top] for n in names}

    def assign(name: str) -> int:
        if name in level:
            return level[name]
        level[name] = 0  # a cycle would otherwise recurse forever
        level[name] = max((assign(d) for d in parents[name]), default=-1) + 1
        return level[name]

    rest = [n for n in names if n not in top]
    for name in rest:
        assign(name)
    # A node with no inputs sits just above its first consumer, not at the top.
    for name in rest:
        if not parents[name] and children[name]:
            level[name] = min(level[c] for c in children[name]) - 1
    low = min((level[n] for n in rest), default=0)
    shifted = {name: level[name] - low + (1 if top else 0) for name in rest}
    return {**dict.fromkeys(top, 0), **shifted}


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


def _clear_of(
    port: float, taken: Sequence[float], span: tuple[float, float], spacing: float
) -> float:
    """``port``, or the nearest point of ``span`` that is ``spacing`` from every taken point."""
    if all(abs(port - t) >= spacing for t in taken):
        return port
    steps = [port + sign * k * spacing / 2 for k in range(1, 40) for sign in (1, -1)]
    for candidate in steps:
        if span[0] <= candidate <= span[1] and all(abs(candidate - t) >= spacing for t in taken):
            return candidate
    return port


def _share_ports(
    plans: dict[tuple[str, str], Plan],
    out_edges: Mapping[str, list[tuple[str, str]]],
    out_range: Mapping[str, tuple[float, float]],
    depart: dict[tuple[str, str], float],
    level: Mapping[str, int],
    fixed: set[tuple[str, str]],
) -> None:
    """One line leaves a source: edges that run in one column under it share their port.

    Two edges of one source that drop in the same column are one trunk that forks later. They
    must not leave at two ports and join in that column, which would look like a merge.
    """
    for source, group in out_edges.items():
        by_column: dict[float, list[tuple[str, str]]] = {}
        for key in group:
            plan = plans.get(key)
            if plan is None or key in fixed:
                continue
            if plan.entry:  # an edge into a side face drops down its channel
                by_column.setdefault(round(plan.channel, 1), []).append(key)
            elif len(plan.columns) > _PORT_AND_ARRIVAL:
                by_column.setdefault(round(plan.columns[1], 1), []).append(key)
        low, high = out_range[source]
        for column, keys in by_column.items():
            if len(keys) == 1:
                continue
            port = min(max(column, low), high)
            for key in keys:
                plan = plans[key]
                jogs = [j for j in plan.jogs if j.gap != level[source]]
                if abs(port - column) >= _EPS:
                    jogs.insert(0, Jog(level[source], port, column))
                plans[key] = replace(plan, columns=(port, *plan.columns[1:]), jogs=tuple(jogs))
                depart[key] = port


def _cap_side_entries(first: Taken, sizes: Mapping[str, LNode], settings: LayoutSettings) -> None:
    """A face holds only as many side entries as fit a lane apart: the others end on top."""
    entering: dict[tuple[str, int], list[tuple[str, str]]] = {}
    for key, plan in first.plans.items():
        if plan.entry:
            entering.setdefault((key[1], plan.entry), []).append(key)
    for (target, _side), keys in entering.items():
        height = sizes[target].height
        room = height - 2 * min(12.0, height / 4)
        capacity = max(1, int(room / settings.slot) + 1)
        keys.sort(key=lambda k: abs(first.plans[k].channel - first.plans[k].face_x))
        for key in keys[capacity:]:
            del first.plans[key]


def _next_column(plan: Plan) -> float:
    """The column an edge drops in after it leaves its source."""
    if plan.entry:
        return plan.channel
    columns = plan.columns
    return columns[min(1, len(columns) - 1)]


def _order_ports(
    plans: dict[tuple[str, str], Plan],
    out_edges: Mapping[str, list[tuple[str, str]]],
    depart: dict[tuple[str, str], float],
    level: Mapping[str, int],
    fixed: set[tuple[str, str]],
) -> None:
    """The ports of one source go in the order of the columns their edges drop in: no crossing."""
    for source, group in out_edges.items():
        keys = [k for k in group if k in plans and k not in fixed]
        if len(keys) == 1:
            continue
        by_port: dict[float, list[tuple[str, str]]] = {}
        for key in keys:
            by_port.setdefault(round(plans[key].columns[0], 1), []).append(key)
        ordered = sorted(by_port.values(), key=lambda ks: min(_next_column(plans[k]) for k in ks))
        for port, members in zip(sorted(by_port), ordered, strict=True):
            for key in members:
                plan = plans[key]
                if abs(plan.columns[0] - port) < _EPS:
                    continue
                jogs = [j for j in plan.jogs if j.gap != level[source]]
                ahead = _next_column(plan)
                if abs(port - ahead) >= _EPS:
                    jogs.insert(0, Jog(level[source], port, ahead))
                plans[key] = replace(plan, columns=(port, *plan.columns[1:]), jogs=tuple(jogs))
                depart[key] = port


def layout(
    nodes: Sequence[LNode],
    edges: Sequence[tuple[str, str]],
    *,
    output: LNode | None = None,
    final: str | None = None,
    top: Sequence[str] = (),
    settings: LayoutSettings | None = None,
) -> Layout:
    """Place ``nodes`` and route ``edges``.

    ``output`` is a node below ``final``, joined by a dashed edge. The nodes in ``top`` (that have
    no inputs) share the first row, ordered by where their consumers are, and every other node is
    below that row.
    """
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
    top_row = frozenset(n for n in top if n in sizes and not inputs_of.get(n))
    level = _levels(names, inputs_of, consumers_of, top_row)

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
    if len(top_row) > 1:
        # Left to right by the median x of their consumers, so the edges leave without crossing.
        def median_consumer(name: str) -> float:
            xs = sorted(x[c] for c in children[name])
            return xs[len(xs) // 2] if xs else x[name]

        reordered = sorted(order[0], key=median_consumer)
        if reordered != order[0]:
            order[0] = reordered
            x = _place_x(order, left, right, virtual, parents, children, settings)

    # Ports: departures spread along the source's bottom edge, arrivals along the target's top edge.
    out_edges: dict[str, list[tuple[str, str]]] = {}
    in_edges: dict[str, list[tuple[str, str]]] = {}
    for key, chain in chains.items():
        out_edges.setdefault(chain[0], []).append(key)
        in_edges.setdefault(chain[-1], []).append(key)
    depart: dict[tuple[str, str], float] = {}
    arrive: dict[tuple[str, str], float] = {}
    out_range: dict[str, tuple[float, float]] = {}
    in_range: dict[str, tuple[float, float]] = {}
    # Edges whose departure or arrival a node asked for: nothing moves that port.
    fixed_out: set[tuple[str, str]] = set()
    fixed_in: set[tuple[str, str]] = set()
    for src, group in out_edges.items():
        group.sort(key=lambda k: x[chains[k][1]])
        half = sizes[src].width / 2 * (1 - 2 * settings.port_margin)
        out_range[src] = (x[src] - half, x[src] + half)
        exit_at = sizes[src].depart_at
        if exit_at is not None:
            for key in group:
                depart[key] = x[src] + exit_at
                fixed_out.add(key)
            continue
        ports = _spread(len(group), *out_range[src], settings.min_port_spacing)
        depart.update(zip(group, ports, strict=False))
    for dst, group in in_edges.items():
        group.sort(key=lambda k: x[chains[k][-2]])
        node = sizes[dst]
        if node.arrive_lo or node.arrive_hi:
            in_range[dst] = (x[dst] + node.arrive_lo, x[dst] + node.arrive_hi)
        else:
            reach = node.width * (0.5 - settings.port_margin)
            in_range[dst] = (x[dst] - reach, x[dst] + reach)
        asked = dict(node.arrive_at)
        for key in group:
            if chains[key][-2] in asked:  # the edge ends where the node said, not where it fits
                arrive[key] = x[dst] + asked[chains[key][-2]]
                fixed_in.add(key)
        loose = [k for k in group if k not in fixed_in]
        if loose:
            ports = _spread(len(loose), *in_range[dst], settings.min_port_spacing)
            taken = [arrive[k] for k in group if k in fixed_in]
            arrive.update(
                (k, _clear_of(p, taken, in_range[dst], SNAP_SPACING))
                for k, p in zip(loose, ports, strict=False)
            )

    # Rows: the x ranges that boxes cover, which no vertical stretch may enter.
    levels = sorted(order)
    row_blocked: dict[int, list[tuple[float, float]]] = {
        lvl: [(x[n] - left[n], x[n] + right[n]) for n in row if n not in virtual]
        for lvl, row in order.items()
    }

    def column_is_free(column: float, first: int, last: int) -> bool:
        """True when a vertical line at ``column`` meets no box in the rows between two levels."""
        return all(
            not any(low - BOX_MARGIN < column < high + BOX_MARGIN for low, high in row_blocked[row])
            for row in range(first + 1, last)
        )

    # An edge whose two ports can share one x runs straight down: move the ports to that x.
    for key in sorted(chains, key=lambda k: abs(depart[k] - arrive[k])):
        src, dst = key
        if abs(depart[key] - arrive[key]) < _EPS or key in fixed_in or key in fixed_out:
            continue
        room = settings.corner_room
        src_lo, src_hi = x[src] - sizes[src].width / 2 + room, x[src] + sizes[src].width / 2 - room
        dst_lo, dst_hi = in_range[dst]
        if not (sizes[dst].arrive_lo or sizes[dst].arrive_hi):
            dst_lo, dst_hi = (
                x[dst] - sizes[dst].width / 2 + room,
                x[dst] + sizes[dst].width / 2 - room,
            )
        low, high = max(src_lo, dst_lo), min(src_hi, dst_hi)
        if low > high:
            continue
        others = [depart[k] for k in out_edges[src] if k != key]
        others += [arrive[k] for k in in_edges[dst] if k != key]
        wanted = min(max(depart[key], low), high)
        candidates = sorted(
            (low + (high - low) * i / 40 for i in range(41)), key=lambda c: abs(c - wanted)
        )
        for column in candidates:
            if all(abs(column - o) >= SNAP_SPACING for o in others) and column_is_free(
                column, level[src], level[dst]
            ):
                depart[key] = arrive[key] = column
                break

    # Edges that end on the side of their target (rows of boxes: the room a channel may not enter).
    row_boxes: dict[int, list[RowBox]] = {
        lvl: [RowBox(x[n] - left[n], x[n] + right[n], n) for n in row if n not in virtual]
        for lvl, row in order.items()
    }

    def request_for(key: tuple[str, str]) -> Request:
        return Request(
            key,
            level[key[0]],
            level[key[1]],
            depart[key],
            arrive[key],
            tuple(x[v] for v in chains[key][1:-1]),
        )

    side_requests: list[SideRequest] = []
    for key in chains:
        src, dst = key
        if (
            not settings.side_entry
            or key in dashed
            or abs(depart[key] - arrive[key]) < _EPS
            or sizes[dst].left_extra
            or sizes[dst].arrive_at
            or sizes[src].depart_at is not None
        ):
            continue
        reach = sizes[src].width / 2 - settings.corner_room
        side_requests.append(
            SideRequest(
                key,
                level[src],
                level[dst],
                (x[src] - max(reach, 0.0), x[src] + max(reach, 0.0)),
                x[src],
                x[dst] - sizes[dst].width / 2,
                x[dst] + sizes[dst].width / 2,
            )
        )
    first = plan_first([request_for(k) for k in chains], row_blocked, side_requests, row_boxes)
    _cap_side_entries(first, sizes, settings)
    # The edges still to route end on top: spread their ports again, now that others left.
    top_edges = [k for k in chains if k not in first.plans]
    for dst, group in in_edges.items():
        rest = [k for k in group if k in top_edges and k not in fixed_in]
        if not rest or len(rest) == len(group):
            continue
        # An edge on a side face takes no place on the top edge. The others keep their port.
        taken_ports = [
            arrive[k]
            for k in group
            if k not in rest and not (k in first.plans and first.plans[k].entry)
        ]
        ports = _spread(len(rest), *in_range[dst], settings.min_port_spacing)
        for key, port in zip(rest, ports, strict=False):
            arrive[key] = _clear_of(port, taken_ports, in_range[dst], SNAP_SPACING)
            taken_ports.append(arrive[key])
    for src, group in out_edges.items():
        rest = [k for k in group if k in top_edges and k not in fixed_out]
        if not rest or len(rest) == len(group):
            continue
        taken_ports = [
            *first.departures.get(src, []),
            *(depart[k] for k in group if k in fixed_out),
        ]
        ports = _spread(len(rest), *out_range[src], settings.min_port_spacing)
        for key, port in zip(rest, ports, strict=False):
            depart[key] = _clear_of(port, taken_ports, out_range[src], SNAP_SPACING)
            taken_ports.append(depart[key])
    plans = plan_rest([request_for(k) for k in top_edges], row_blocked, first)
    for key, plan in first.plans.items():
        if plan.entry:
            depart[key] = plan.columns[0]
    # A tiny sideways move next to a port is a kink: move the port to the column instead.
    for key, plan in list(plans.items()):
        if plan.entry or key in fixed_in or key in fixed_out:
            continue
        src, dst = key
        columns, jogs = list(plan.columns), list(plan.jogs)
        last_gap = level[dst] - 1
        if jogs and jogs[-1].gap == last_gap and abs(jogs[-1].x_to - jogs[-1].x_from) <= KINK:
            moved = jogs[-1].x_from
            low, high = in_range[dst]
            near = [arrive[k] for k in in_edges[dst] if k != key]
            if low <= moved <= high and all(abs(moved - o) >= SNAP_SPACING for o in near):
                arrive[key] = columns[-1] = moved
                jogs.pop()
        if jogs and jogs[0].gap == level[src] and abs(jogs[0].x_to - jogs[0].x_from) <= KINK:
            moved = jogs[0].x_to
            low, high = out_range[src]
            near = [depart[k] for k in out_edges[src] if k != key]
            if low <= moved <= high and all(abs(moved - o) >= SNAP_SPACING for o in near):
                depart[key] = columns[0] = moved
                jogs.pop(0)
        plans[key] = Plan(key, tuple(columns), tuple(jogs))
    _share_ports(plans, out_edges, out_range, depart, level, fixed_in | fixed_out)
    _order_ports(plans, out_edges, depart, level, fixed_in | fixed_out)
    slot_of, slot_count = assign_slots(list(plans.values()))

    # Row heights, then the gap under each row: it grows with the slots it must hold.
    row_height: dict[int, float] = {}
    for lvl, row in order.items():
        real = [sizes[n].height + sizes[n].top_extra for n in row if n in sizes]
        row_height[lvl] = max(real) if real else 0.0
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
                box_top = row_top[lvl] + (row_height[lvl] - full) / 2 + node.top_extra
                top_of_box[n] = box_top
                y_center[n] = box_top + node.height / 2

    jog_y: dict[tuple[tuple[str, str], int], float] = {}
    for gap, count in slot_count.items():
        bottom = row_top[gap] + row_height[gap]
        low = bottom + settings.slot * 2
        high = bottom + gap_after[gap] - settings.slot * 2 - settings.arrow
        positions = _spread(count, low, max(low, high), settings.slot)
        for (key, index), number in slot_of.items():
            if plans[key].jogs[index].gap == gap:
                jog_y[(key, index)] = positions[number]

    # Where edges enter a side face: along the face, the farthest channel lowest (no crossings).
    side_y: dict[tuple[str, str], float] = {}
    entering: dict[tuple[str, int], list[tuple[str, str]]] = {}
    for key, plan in plans.items():
        if plan.entry:
            entering.setdefault((key[1], plan.entry), []).append(key)
    for (dst, _face_side), group in entering.items():
        group.sort(key=lambda k: abs(plans[k].channel - plans[k].face_x))
        top_edge = top_of_box[dst]
        margin = min(12.0, sizes[dst].height / 4)
        ys = _spread(
            len(group),
            top_edge + margin,
            top_edge + sizes[dst].height - margin,
            settings.min_port_spacing,
        )
        side_y.update(zip(group, ys, strict=False))

    routes: list[Route] = []
    for key, plan in plans.items():
        src, dst = key
        points: list[Point] = [(depart[key], top_of_box[src] + sizes[src].height)]
        for index, jog in enumerate(plan.jogs):
            y = jog_y[(key, index)]
            points += [(jog.x_from, y), (jog.x_to, y)]
        if plan.entry:
            points += [(plan.channel, side_y[key]), (plan.face_x, side_y[key])]
        else:
            points.append((arrive[key], top_of_box[dst]))
        routes.append(Route(src, dst, tuple(points), key in dashed))

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
        Route(r.source, r.target, tuple((px + dx, py + dy) for px, py in r.points), r.dashed)
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
