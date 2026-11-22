"""The rules two edges follow: branch, never merge; different sources keep a lane apart.

``violations(svg)`` parses a drawn SVG and returns one text per broken rule. The rules:

1. Edges of different sources never run side by side closer than ``LANE`` px (they may cross).
2. Edges of one source share only a stretch that starts at the source, and once they part they do
   not come close again (a tree, no merge).
3. Edges into one target end at points at least ``ARRIVAL`` px apart.
"""

from __future__ import annotations

import itertools
import math
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property

from test_svg_layout import path_points

NS = "{http://www.w3.org/2000/svg}"
LANE = 12.0
TOLERANCE = 0.8  # a path is drawn with one decimal
SAME = 1.5  # two lines closer than this are one line (the samples of two paths are offset)
ARRIVAL = 10.0
STEP = 2.0
PARALLEL = 30.0  # a run this long, closer than a lane, is a parallel run and not a crossing
SHARED = 8.0  # a stretch longer than this where two edges are one line is a trunk
FORK = 44.0  # two edges of one source need this long to part from each other
NEAR_END = 16.0  # the last stretch next to an arrowhead

Point = tuple[float, float]


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    points: tuple[Point, ...]  # dense: one every ``STEP`` px

    @property
    def tip(self) -> Point:
        return self.points[-1]

    @cached_property
    def box(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return min(xs), min(ys), max(xs), max(ys)

    def near(self, other: Edge) -> bool:
        """False when the two edges are farther apart than a lane everywhere (a quick test)."""
        a, b = self.box, other.box
        return not (
            a[2] + LANE < b[0] or b[2] + LANE < a[0] or a[3] + LANE < b[1] or b[3] + LANE < a[1]
        )

    @cached_property
    def cells(self) -> dict[tuple[int, int], list[Point]]:
        """The points in a grid of ``LANE`` px cells, to find the nearest one fast."""
        grid: dict[tuple[int, int], list[Point]] = {}
        for p in self.points:
            grid.setdefault((int(p[0] // LANE), int(p[1] // LANE)), []).append(p)
        return grid


def _dense(points: tuple[Point, ...]) -> tuple[Point, ...]:
    out: list[Point] = [points[0]]
    for a, b in itertools.pairwise(points):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        steps = max(1, round(length / STEP))
        out.extend(
            (a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps)
            for i in range(1, steps + 1)
        )
    return tuple(out)


def read_edges(svg: str) -> list[Edge]:
    root = ET.fromstring(svg)  # noqa: S314 (adag's own output)
    edges: list[Edge] = []
    for path in root.iter(f"{NS}path"):
        classes = path.get("class", "").split()
        if "dag-edge" not in classes or "dag-edge--feedback" in classes:
            continue
        if "dag-edge--output" in classes or path.get("data-source") is None:
            continue
        points = path_points(path.get("d", ""))
        if len(points) >= 2:
            edges.append(
                Edge(path.get("data-source", ""), path.get("data-target", ""), _dense(points))
            )
    return edges


def _distance(p: Point, edge: Edge) -> float:
    """The distance from ``p`` to the nearest point of ``edge``, or ``LANE`` when it is farther."""
    cx, cy = int(p[0] // LANE), int(p[1] // LANE)
    best = LANE
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for q in edge.cells.get((cx + dx, cy + dy), ()):
                best = min(best, math.hypot(p[0] - q[0], p[1] - q[1]))
    return best


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    """The ``(first, last)`` indices of each stretch of consecutive true flags."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = i
        if not flag and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(flags) - 1))
    return runs


def _length(run: tuple[int, int]) -> float:
    return (run[1] - run[0] + 1) * STEP


def _crosses(points: tuple[Point, ...], other: Edge) -> bool:
    """True when the polyline ``points`` cuts the edge ``other`` (a crossing, not a parallel run)."""
    for p, q in itertools.pairwise(points):
        cx, cy = int((p[0] + q[0]) / 2 // LANE), int((p[1] + q[1]) / 2 // LANE)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                near = other.cells.get((cx + dx, cy + dy), ())
                if any(_side(p, q, r) * _side(p, q, t) < 0 for r, t in _pairs(other, near)):
                    return True
    return False


def _side(p: Point, q: Point, r: Point) -> float:
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def _pairs(edge: Edge, near: Sequence[Point]) -> list[tuple[Point, Point]]:
    """Consecutive points of ``edge`` that start at one of the ``near`` points."""
    index = {p: i for i, p in enumerate(edge.points)}
    out: list[tuple[Point, Point]] = []
    for p in near:
        i = index[p]
        if i + 1 < len(edge.points):
            out.append((p, edge.points[i + 1]))
    return out


def _parallel_run(a: Edge, b: Edge, limit: int | None = None) -> float:
    """The longest stretch of ``a`` closer than a lane to ``b`` that is not a crossing."""
    points = a.points[: len(a.points) if limit is None else max(limit, 0)]
    flags = [_distance(p, b) < LANE - TOLERANCE for p in points]
    best = 0.0
    for run in _runs(flags):
        if _length(run) > best and not _crosses(points[run[0] : run[1] + 2], b):
            best = _length(run)
    return best


def _run_of_different_sources(a: Edge, b: Edge) -> float:
    """The longest stretch of ``a`` near ``b``, away from the arrowheads they share."""
    limit = len(a.points) - int(NEAR_END / STEP) if a.target == b.target else None
    return _parallel_run(a, b, limit)


def _shared_prefix_broken(a: Edge, b: Edge) -> str:
    """Why two edges of one source are not a tree, or an empty text when they are."""
    same = [_distance(p, b) < SAME for p in a.points]
    # A short touch is a crossing. A stretch that is one line is a shared trunk.
    trunks = [run for run in _runs(same) if _length(run) > SHARED]
    if not trunks:
        if _parallel_run(a, b) > PARALLEL:
            return "run side by side without sharing a trunk"
        return ""
    if len(trunks) > 1 or trunks[0][0] > 1:
        return "share a stretch that does not start at the source"
    after = trunks[0][1] + int(FORK / STEP)
    rest = a.points[after:]
    flags = [_distance(p, b) < LANE - TOLERANCE for p in rest]
    if any(
        _length(run) > PARALLEL and not _crosses(rest[run[0] : run[1] + 2], b)
        for run in _runs(flags)
    ):
        return "come close again after they part (a merge)"
    return ""


def violations(svg: str) -> list[str]:
    edges = read_edges(svg)
    found: list[str] = []
    for a, b in ((x, y) for i, x in enumerate(edges) for y in edges[i + 1 :]):
        if not a.near(b):
            continue
        if a.source != b.source:
            run = max(_run_of_different_sources(a, b), _run_of_different_sources(b, a))
            if run > PARALLEL:
                found.append(
                    f"{a.source}>{a.target} and {b.source}>{b.target} run {run:.0f} px close"
                )
        else:
            why = _shared_prefix_broken(a, b)
            if not why:
                why = _shared_prefix_broken(b, a)
            if why:
                found.append(f"{a.source}>{a.target} and {b.source}>{b.target} {why}")
    by_target: dict[str, list[Edge]] = {}
    for edge in edges:
        by_target.setdefault(edge.target, []).append(edge)
    for target, group in by_target.items():
        for a, b in ((x, y) for i, x in enumerate(group) for y in group[i + 1 :]):
            if math.hypot(a.tip[0] - b.tip[0], a.tip[1] - b.tip[1]) < ARRIVAL - TOLERANCE:
                found.append(
                    f"{a.source}>{target} and {b.source}>{target} arrive {ARRIVAL:.0f} px close"
                )
    return found
