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
from collections.abc import Mapping, Sequence
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
    """The columns of one edge, one per row it crosses (first and last are its ports)."""

    key: tuple[str, str]
    columns: tuple[float, ...]
    jogs: tuple[Jog, ...]

    @property
    def bends(self) -> int:
        return 2 * len(self.jogs)


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


def plan_all(
    requests: Sequence[Request], row_blocked: Mapping[int, Blocked]
) -> dict[tuple[str, str], Plan]:
    """Plan every edge. The straight ones go first, so a bent edge keeps off their columns."""
    plans: dict[tuple[str, str], Plan] = {}
    occupied: dict[int, list[tuple[float, str]]] = {}
    ordered = sorted(
        requests,
        key=lambda r: (abs(r.source_x - r.target_x) >= _SAME, r.target_level - r.source_level),
    )
    for request in ordered:
        plans[request.key] = plan_edge(request, row_blocked, occupied)
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
