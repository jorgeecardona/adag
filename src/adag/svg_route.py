"""Route each edge on its own: fewest bends first, then the shortest way.

An edge goes down from the bottom of its source to the top of its target. A bend is a sideways
move, and it always happens in the gap between two rows, never through a box. Each edge is planned
alone: a straight drop when it is free, else one sideways move (a Z) placed in the gap just above
the target when it can, and a second sideways move only when a box blocks the plain Z.

The plan is a column ``x`` for every row the edge crosses. The sideways moves are where two columns
differ. The layout turns the plan into points once it knows the height of each gap.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

BOX_MARGIN = 8.0
"""A vertical stretch stays this far from a box (the test asks for 6)."""
LANE_GAP = 4.0
"""Two vertical stretches of different sources in one row stay this far apart."""
_BEND = 1000.0
_CONFLICT = 50.0
_SAME = 0.5
_BIG = 1.0e9
_EDGE_STEP = 1.0
_POOL = 36


@dataclass(frozen=True)
class Request:
    """One edge to route: its ends, and where its virtual nodes sit (a hint for a free lane)."""

    key: tuple[str, str]
    source_level: int
    target_level: int
    source_x: float
    target_x: float
    lane_hint: tuple[float, ...] = ()


@dataclass(frozen=True)
class Jog:
    """A sideways move from ``x_from`` to ``x_to`` in the gap under row ``gap``."""

    gap: int
    x_from: float
    x_to: float


@dataclass(frozen=True)
class Plan:
    """The columns of one edge, one per row it crosses (first and last are its ports).

    ``entry`` is 0 when the edge ends on the top of its target. It is -1 or 1 when the edge ends on
    the left or right face of the target: it then drops down the column ``channel`` to the centre
    of the target and goes sideways into ``face_x``. ``columns`` holds the departure column.
    """

    key: tuple[str, str]
    columns: tuple[float, ...]
    jogs: tuple[Jog, ...]
    entry: int = 0
    channel: float = 0.0
    face_x: float = 0.0

    @property
    def bends(self) -> int:
        return 2 * len(self.jogs) + (1 if self.entry else 0)


Blocked = Sequence[tuple[float, float]]
"""For one row: the x ranges that boxes cover."""


def _free_intervals(blocked: Blocked) -> list[tuple[float, float]]:
    """The x ranges of a row that no box covers (with a margin), from -inf to +inf."""
    spans = sorted((left - BOX_MARGIN, right + BOX_MARGIN) for left, right in blocked)
    merged: list[tuple[float, float]] = []
    for left, right in spans:
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    free: list[tuple[float, float]] = []
    cursor = -_BIG
    for left, right in merged:
        if left > cursor:
            free.append((cursor, left))
        cursor = max(cursor, right)
    free.append((cursor, _BIG))
    return free


def _in_free(x: float, free: list[tuple[float, float]]) -> bool:
    return any(low <= x <= high for low, high in free)


def _pool(
    free_rows: dict[int, list[tuple[float, float]]],
    hints: Sequence[float],
    near: tuple[float, float],
) -> list[float]:
    """Columns worth trying: the hints, and the edges and middles of every free lane of the rows.

    A column that is free in every row can then be found, even when no row's own lanes line up.
    """
    found: set[float] = set(hints)
    for free in free_rows.values():
        for low, high in free:
            found.update(min(max(hint, low), high) for hint in hints)
            if low > -_BIG:
                found.add(low + _EDGE_STEP)
            if high < _BIG:
                found.add(high - _EDGE_STEP)
            if high - low < _BIG:
                found.add((low + high) / 2)
    centre = (near[0] + near[1]) / 2
    spread = (near[1] - near[0]) / 2
    ranked = sorted(found, key=lambda v: max(0.0, abs(v - centre) - spread))
    return sorted(ranked[:_POOL])


def plan_edge(
    request: Request,
    row_blocked: Mapping[int, Blocked],
    occupied: dict[int, list[tuple[float, str]]],
) -> Plan:
    """Plan one edge: the columns with the fewest sideways moves, then the least sideways travel."""
    first, last = request.source_level, request.target_level
    rows = list(range(first + 1, last))
    hints = [request.source_x, request.target_x, *request.lane_hint]
    free_rows = {row: _free_intervals(row_blocked.get(row, ())) for row in rows}
    near = (min(request.source_x, request.target_x), max(request.source_x, request.target_x))
    # A column a little to the side of one that is taken lets a second edge run beside the first.
    beside = [
        other + side * (LANE_GAP + _EDGE_STEP)
        for row in rows
        for other, _ in occupied.get(row, ())
        for side in (-1, 1)
    ]
    pool = _pool(free_rows, [*hints, *beside], near)
    candidates: dict[int, list[float]] = {}
    for row in rows:
        own = {x for x in pool if _in_free(x, free_rows[row])}
        for low, high in free_rows[row]:  # always at least one column per row
            own.update(min(max(hint, low), high) for hint in hints)
        candidates[row] = sorted(own)

    # best[row][x] = (cost, previous x); the cost counts sideways moves first.
    best: dict[int, dict[float, tuple[float, float]]] = {first: {request.source_x: (0.0, 0.0)}}
    previous_row = first
    for row in [*rows, last]:
        options = [request.target_x] if row == last else candidates[row]
        layer: dict[float, tuple[float, float]] = {}
        for x in options:
            conflict = 0.0
            if row != last:
                conflict = sum(
                    _CONFLICT
                    for other, source in occupied.get(row, ())
                    if abs(other - x) < LANE_GAP and not (source == request.key[0] and other == x)
                )
            choice: tuple[float, float] | None = None
            for prior, (cost, _) in best[previous_row].items():
                step = 0.0
                if abs(prior - x) >= _SAME:
                    # Later gaps are slightly cheaper: the move prefers the gap above the target.
                    step = _BEND + abs(prior - x) * 0.01 + (last - 1 - previous_row) * 0.1
                total = cost + step + conflict
                if choice is None or total < choice[0]:
                    choice = (total, prior)
            if choice is not None:
                layer[x] = choice
        best[row] = layer
        previous_row = row

    # Walk back from the target.
    columns: list[float] = [request.target_x]
    x = request.target_x
    for row in [last, *reversed(rows)]:
        x = best[row][x][1]
        columns.append(x)
    columns.reverse()
    jogs = tuple(
        Jog(first + i, a, b)
        for i, (a, b) in enumerate(itertools.pairwise(columns))
        if abs(a - b) >= _SAME
    )
    for row, column in zip(rows, columns[1:-1], strict=True):
        occupied.setdefault(row, []).append((column, request.key[0]))
    return Plan(request.key, tuple(columns), jogs)


@dataclass(frozen=True)
class RowBox:
    """A box in a row: the x range it covers, and its node name."""

    low: float
    high: float
    name: str


@dataclass(frozen=True)
class SideRequest:
    """An edge that may end on the side of its target.

    ``span`` is where the edge may leave the bottom of its source.
    """

    key: tuple[str, str]
    source_level: int
    target_level: int
    span: tuple[float, float]
    source_x: float
    target_left: float
    target_right: float


SPACING = 10.0
"""Departure points on one source bottom stay this far apart."""
HMIN = 24.0
"""The sideways run into a target is at least this long (a bend and an arrowhead need it)."""
SIDE_MARGIN = 6.0
"""A channel stays this far from a box."""
_STEP = 3.0
_REACH_LIMIT = 160.0


def _face(channel: float, request: SideRequest) -> int:
    """-1 when the channel is left of the target, 1 when right of it, else 0."""
    if channel <= request.target_left - HMIN:
        return -1
    if channel >= request.target_right + HMIN:
        return 1
    return 0


def _channel_free(
    channel: float,
    request: SideRequest,
    rows: Mapping[int, Sequence[RowBox]],
    occupied: Mapping[int, list[tuple[float, str]]],
) -> bool:
    """True when a drop down ``channel`` to the target's side meets no box and no other edge."""
    face = _face(channel, request)
    if face == 0:
        return False
    face_x = request.target_left if face < 0 else request.target_right
    target = request.key[1]
    for row in range(request.source_level + 1, request.target_level + 1):
        for box in rows.get(row, ()):
            if row == request.target_level and box.name == target:
                continue
            if box.low - SIDE_MARGIN < channel < box.high + SIDE_MARGIN:
                return False
            if (
                row == request.target_level
                and box.low < max(channel, face_x)
                and box.high > min(channel, face_x)
            ):
                return False
        for other, source in occupied.get(row, ()):
            if abs(other - channel) < LANE_GAP and not (
                source == request.key[0] and other == channel
            ):
                return False
    return True


def _samples(low: float, high: float) -> list[float]:
    count = max(1, int((high - low) / _STEP))
    return [low + (high - low) * i / count for i in range(count + 1)] if high > low else [low]


def _departure(
    wanted: float, span: tuple[float, float], spaced: Callable[[float], bool]
) -> float | None:
    """The point of ``span`` nearest to ``wanted`` that ``spaced`` accepts, or None."""
    start = min(max(wanted, span[0]), span[1])
    for k in range(24):
        for candidate in [start] if k == 0 else [start - k * SPACING / 2, start + k * SPACING / 2]:
            if span[0] <= candidate <= span[1] and spaced(candidate):
                return candidate
    return None


def plan_sides(
    requests: Sequence[SideRequest],
    rows: Mapping[int, Sequence[RowBox]],
    occupied: dict[int, list[tuple[float, str]]],
    taken: dict[str, list[float]],
) -> dict[tuple[str, str], Plan]:
    """Give an edge a side entry when it can: an L from the source bottom, else one jog and an L.

    An L leaves the source bottom at ``c``, drops straight down to the centre of the target and
    goes sideways into its face. The edges that leave one source are ordered by target, so their
    drops do not cross. An edge that no drop can serve is left out: the caller routes it on top.
    """
    plans: dict[tuple[str, str], Plan] = {}

    def register(plan: Plan, request: SideRequest, column: float) -> None:
        for row in range(request.source_level + 1, request.target_level + 1):
            occupied.setdefault(row, []).append((column, request.key[0]))
        taken.setdefault(request.key[0], []).append(plan.columns[0])
        plans[request.key] = plan

    def spaced(source: str, x: float) -> bool:
        return all(abs(x - other) >= SPACING for other in taken.get(source, ()))

    by_source: dict[str, list[SideRequest]] = {}
    for request in requests:
        by_source.setdefault(request.key[0], []).append(request)
    unresolved: list[SideRequest] = []
    for source, group in by_source.items():
        right = sorted(
            (r for r in group if (r.target_left + r.target_right) / 2 >= r.source_x),
            key=lambda r: (r.target_level, r.target_left),
        )
        left = sorted(
            (r for r in group if (r.target_left + r.target_right) / 2 < r.source_x),
            key=lambda r: (r.target_level, -r.target_right),
        )
        cursor = float("inf")
        for request in right:
            found = [
                c
                for c in _samples(*request.span)
                if c <= cursor
                and _face(c, request) < 0
                and spaced(source, c)
                and _channel_free(c, request, rows, occupied)
            ]
            if not found:
                unresolved.append(request)
                continue
            c = max(found)
            face_x = request.target_left
            register(Plan(request.key, (c,), (), -1, c, face_x), request, c)
            cursor = c - SPACING
        cursor = float("-inf")
        for request in left:
            found = [
                c
                for c in _samples(*request.span)
                if c >= cursor
                and _face(c, request) > 0
                and spaced(source, c)
                and _channel_free(c, request, rows, occupied)
            ]
            if not found:
                unresolved.append(request)
                continue
            c = min(found)
            register(Plan(request.key, (c,), (), 1, c, request.target_right), request, c)
            cursor = c + SPACING

    # One jog under the source, then the drop. The shortest sideways travel wins.
    xs = sorted({b.low for boxes in rows.values() for b in boxes})
    xs += sorted({b.high for boxes in rows.values() for b in boxes})
    lo, hi = (min(xs) - 60, max(xs) + 60) if xs else (0.0, 0.0)
    for request in sorted(unresolved, key=lambda r: r.target_level - r.source_level):
        source = request.key[0]
        best: tuple[float, float, float] | None = None
        for c in _samples(lo, hi):
            face = _face(c, request)
            if face == 0 or not _channel_free(c, request, rows, occupied):
                continue
            depart = _departure(c, request.span, lambda m, source=source: spaced(source, m))
            if depart is None:
                continue
            face_x = request.target_left if face < 0 else request.target_right
            travel = abs(depart - c) + abs(c - face_x)
            if best is None or travel < best[0]:
                best = (travel, c, depart)
        direct = abs(request.source_x - (request.target_left + request.target_right) / 2)
        if best is None or best[0] > direct + _REACH_LIMIT:
            continue
        _, c, depart = best
        face = _face(c, request)
        face_x = request.target_left if face < 0 else request.target_right
        jogs = () if abs(depart - c) < _SAME else (Jog(request.source_level, depart, c),)
        register(Plan(request.key, (depart,), jogs, face, c, face_x), request, c)
    return plans


@dataclass
class Taken:
    """What the first stage planned: the plans, the columns in use and the departure points."""

    plans: dict[tuple[str, str], Plan]
    occupied: dict[int, list[tuple[float, str]]]
    departures: dict[str, list[float]]


def plan_first(
    requests: Sequence[Request],
    row_blocked: Mapping[int, Blocked],
    sides: Sequence[SideRequest],
    rows: Mapping[int, Sequence[RowBox]],
) -> Taken:
    """Plan the straight edges, then the edges that can end on a side of their target."""
    plans: dict[tuple[str, str], Plan] = {}
    occupied: dict[int, list[tuple[float, str]]] = {}
    departures: dict[str, list[float]] = {}
    straight = [r for r in requests if abs(r.source_x - r.target_x) < _SAME]
    for request in sorted(straight, key=lambda r: r.target_level - r.source_level):
        plans[request.key] = plan_edge(request, row_blocked, occupied)
        departures.setdefault(request.key[0], []).append(request.source_x)
    plans.update(plan_sides([r for r in sides if r.key not in plans], rows, occupied, departures))
    return Taken(plans, occupied, departures)


def plan_rest(
    requests: Sequence[Request], row_blocked: Mapping[int, Blocked], first: Taken
) -> dict[tuple[str, str], Plan]:
    """Plan the edges that end on the top of their target, keeping off the columns taken."""
    plans = dict(first.plans)
    for request in sorted(requests, key=lambda r: r.target_level - r.source_level):
        plans[request.key] = plan_edge(request, row_blocked, first.occupied)
    return plans


def assign_slots(
    plans: Sequence[Plan],
) -> tuple[dict[tuple[tuple[str, str], int], int], dict[int, int]]:
    """Give each sideways move a slot (a y) in its gap, so runs that overlap in x do not touch.

    Returns the slot of each ``(edge, jog index)`` and the number of slots in each gap.
    """
    by_gap: dict[int, list[tuple[tuple[str, str], int, Jog]]] = {}
    for plan in plans:
        for index, jog in enumerate(plan.jogs):
            by_gap.setdefault(jog.gap, []).append((plan.key, index, jog))
    slot_of: dict[tuple[tuple[str, str], int], int] = {}
    count: dict[int, int] = {}
    for gap, jogs in by_gap.items():
        # The run that ends furthest right goes highest, so the verticals of the others cross less.
        ordered = sorted((j for j in jogs if j[2].x_to > j[2].x_from), key=lambda j: -j[2].x_to)
        ordered += sorted((j for j in jogs if j[2].x_to < j[2].x_from), key=lambda j: j[2].x_to)
        slots: list[list[tuple[float, float]]] = []
        for key, index, jog in ordered:
            span = (min(jog.x_from, jog.x_to) - LANE_GAP, max(jog.x_from, jog.x_to) + LANE_GAP)
            for number, taken in enumerate(slots):
                if all(span[1] < low or span[0] > high for low, high in taken):
                    taken.append(span)
                    slot_of[(key, index)] = number
                    break
            else:
                slots.append([span])
                slot_of[(key, index)] = len(slots) - 1
        count[gap] = len(slots)
    return slot_of, count
