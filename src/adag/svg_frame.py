# pyright: reportPrivateUsage=false
"""Frames: the body of a fan-out or a loop, drawn once inside a dashed box around it.

A frame is a node that other nodes name as their ``parent``. Its body is laid out with the same
engine as the whole graph (in the compact density, with no cache state), and the frame is then
one box of the outer layout. The edges that enter the frame end on the inputs of the template at
the top of the body, and one edge leaves the bottom of the box, under the step that ends the body.
A loop also gets a dashed edge back to its ``state`` input: the one place where adag draws a
cycle, and it is painted by the frame, never routed by the layout.

Nothing here reads the cache or the template: it draws the nodes and edges it is given.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from adag import svg_layout as lay
from adag import svg_paint as paint
from adag.svg_paint import Look, Measured, RNode

MAX_DEPTH = 3
"""A frame inside a frame inside a frame is drawn. A fourth level stays one dashed box."""

_PAD = 14.0  # left, right and bottom padding of a frame
_TAG_ROW = 26.0  # the strip at the top that holds the tag and the state word
_GAP_TOP = 8.0  # between that strip and the first row of the body
_CAPTION_ROW = 22.0
_GUTTER = 34.0  # room on the right for the edge back to the state input of a loop
_LABEL_SIZE = 11
_INPUT_KINDS = ("param", "file", "template_input")

Point = tuple[float, float]


@dataclass(frozen=True)
class Level:
    """The nodes of one level, placed. Coordinates are relative to the frame that holds the level."""

    nodes: tuple[RNode, ...]
    edges: tuple[tuple[str, str], ...]
    placed: dict[str, lay.Placed]
    measured: dict[str, Measured]
    routes: tuple[lay.Route, ...]
    width: float
    height: float
    bodies: dict[str, Body]
    look: Look
    """The look the nodes of this level are drawn with."""


@dataclass(frozen=True)
class Feedback:
    """The dashed edge from the end of a loop body back to its ``state`` input."""

    points: tuple[Point, ...]
    label_at: Point | None
    arrive: Point


@dataclass(frozen=True)
class Body:
    """The inside of one frame, in the frame's own coordinates (the top-left corner is 0, 0)."""

    node: RNode
    level: Level
    width: float
    height: float
    tag: str
    caption_x: float
    exit_x: float | None
    exit_bottom: float
    feedback: Feedback | None = None
    depart_at: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "depart_at", None if self.exit_x is None else self.exit_x - self.width / 2
        )

    def locate(self, name: str) -> Point | None:
        """The x of the centre and the y of the top of node ``name`` in this frame, at any depth."""
        placed = self.level.placed.get(name)
        if placed is not None:
            return placed.x, placed.y - placed.height / 2
        for frame, body in self.level.bodies.items():
            found = body.locate(name)
            if found is not None:
                box = self.level.placed[frame]
                return box.x - box.width / 2 + found[0], box.y - box.height / 2 + found[1]
        return None


@dataclass(frozen=True)
class Ctx:
    """The whole flat graph: who is whose child, and every edge."""

    children: Mapping[str, Sequence[RNode]]
    parent_of: Mapping[str, str]
    edges: Sequence[tuple[str, str]]
    look: Look


# ── Laying out ───────────────────────────────────────────────────────────────


def _body_look(look: Look) -> Look:
    """The look of the inside of a frame: compact, so a nested frame stays small."""
    return replace(look, density="compact")


def _node_look(node: RNode, look: Look) -> Look:
    """A step of a template is not one run: it has no cache state."""
    return replace(look, stateless=True) if node.state == "NONE" else look


def _lift(ctx: Ctx, names: set[str], name: str) -> str | None:
    """The node of this level that holds ``name``, or None when ``name`` lies outside the level."""
    current = name
    while current:
        if current in names:
            return current
        current = ctx.parent_of.get(current, "")
    return None


def build_level(
    ctx: Ctx,
    parent: str,
    depth: int,
    look: Look,
    *,
    margin: float,
    output_label: str = "",
    final: str | None = None,
) -> Level:
    """Place the children of ``parent``. A child with children of its own is a frame."""
    nodes = list(ctx.children.get(parent, ()))
    names = {n.name for n in nodes}
    bodies: dict[str, Body] = {}
    if depth < MAX_DEPTH:
        for n in nodes:
            if n.kind == "step" and n.name in ctx.children:
                bodies[n.name] = build_body(ctx, n, depth + 1)

    level_edges: list[tuple[str, str]] = []
    enter: dict[tuple[str, str], str] = {}
    for a, b in ctx.edges:
        a2, b2 = _lift(ctx, names, a), _lift(ctx, names, b)
        if a2 is None or b2 is None or a2 == b2:
            continue
        if (a2, b2) not in level_edges:
            level_edges.append((a2, b2))
        if b2 in bodies and b != b2:
            enter.setdefault((a2, b2), b)  # this edge ends on an input inside the frame

    fold = look.style != "classic" and look.inputs != "terminals"
    shown, shown_edges = paint._fold_inputs(nodes, level_edges, fold=fold, keep=frozenset(bodies))
    measured: dict[str, Measured] = {}
    sizes: list[lay.LNode] = []
    for n in shown:
        body = bodies.get(n.name)
        if body is None:
            m = paint.measure(n, _node_look(n, look))
            sizes.append(
                lay.LNode(
                    n.name,
                    m.width,
                    m.height,
                    m.left_extra,
                    m.top_extra,
                    m.arrive_lo,
                    m.arrive_hi,
                    n.kind,
                )
            )
        else:
            m = Measured(body.width, body.height)
            arrive: dict[str, float] = {}
            for (source, frame), terminal in enter.items():
                found = body.locate(terminal) if frame == n.name else None
                if found is not None:
                    arrive.setdefault(source, found[0] - body.width / 2)
            sizes.append(
                lay.LNode(
                    n.name,
                    body.width,
                    body.height,
                    kind=n.kind,
                    arrive_at=tuple(arrive.items()),
                    depart_at=body.depart_at,
                )
            )
        measured[n.name] = m

    for n in shown:  # widen a box that has too many edges, but not a frame (its size is its body)
        if n.name not in bodies:
            measured.update(paint.fit_to_ports({n.name: measured[n.name]}, shown_edges))
    sizes = [
        replace(size, width=measured[size.name].width) if size.name not in bodies else size
        for size in sizes
    ]
    fed = {target for _, target in shown_edges}
    top = [n.name for n in shown if n.kind in _INPUT_KINDS and n.name not in fed]
    output = (
        paint.measure_output(output_label, look)
        if output_label and final is not None and final in names
        else None
    )
    settings = paint.settings_for(look)
    result = lay.layout(
        sizes,
        shown_edges,
        output=output,
        final=final,
        top=top,
        settings=lay.LayoutSettings(
            gap_y=settings.gap_y,
            gap_x=settings.gap_x,
            arrow=paint._arrow_size(look.style)[0],
            margin=margin,
            side_entry=look.style != "band",
            slot=paint._BAND_SLOT if look.style == "band" else 12.0,
        ),
    )
    routes = [_into_frame(r, result, bodies, enter) for r in result.routes]
    return Level(
        tuple(shown),
        tuple(shown_edges),
        dict(result.nodes),
        measured,
        tuple(routes),
        result.width,
        result.height,
        bodies,
        look,
    )


def _into_frame(
    route: lay.Route,
    result: lay.Layout,
    bodies: Mapping[str, Body],
    enter: Mapping[tuple[str, str], str],
) -> lay.Route:
    """An edge into a frame goes on, inside the frame, to the top of the input it feeds."""
    body = bodies.get(route.target)
    terminal = enter.get((route.source, route.target))
    if body is None or terminal is None:
        return route
    found = body.locate(terminal)
    if found is None:
        return route
    box = result.nodes[route.target]
    end = (box.x - box.width / 2 + found[0], box.y - box.height / 2 + found[1])
    return replace(route, points=(*route.points[:-1], end))


def build_body(ctx: Ctx, node: RNode, depth: int) -> Body:
    """Lay out the children of frame ``node`` and wrap them in a box with a tag and a caption."""
    look = _body_look(ctx.look)
    inner = build_level(ctx, node.name, depth, look, margin=0.0)
    tag = "loop" if node.shape == "loop" else "each"
    tag_w = paint._text_width(tag, 11, paint._MONO_EM) + 16
    state_w = (
        0.0
        if ctx.look.stateless or not paint._state_word(node, ctx.look)
        else paint._text_width(paint._state_word(node, ctx.look), 11, paint._WORD_EM) + 12
    )

    # The entering edges cross the strip at the top: no input may stand under the tag.
    inputs_x = [
        p.x for name, p in inner.placed.items() if _kind_of(inner, name) == "template_input"
    ]
    ox = max(_PAD, tag_w + 10 - min(inputs_x)) if inputs_x else _PAD
    oy = _TAG_ROW + _GAP_TOP

    sinks = [
        n.name
        for n in inner.nodes
        if n.kind not in _INPUT_KINDS and not any(a == n.name for a, _ in inner.edges)
    ]
    exit_name = sinks[0] if len(sinks) == 1 else None
    exit_x: float | None = None
    exit_bottom = 0.0
    if exit_name is not None:
        end = inner.placed[exit_name]
        # a frame that ends this body is left at its own exit, not at its centre
        nested = inner.bodies.get(exit_name)
        shift = nested.depart_at if nested is not None and nested.depart_at is not None else 0.0
        exit_x = end.x + shift + ox
        exit_bottom = end.y + end.height / 2 + oy

    loop_back = (
        node.shape == "loop"
        and exit_name is not None
        and node.feedback in inner.placed
        and node.feedback != ""
    )
    gutter = _GUTTER if loop_back else 0.0
    width = ox + inner.width + gutter + _PAD
    width = max(width, tag_w + state_w + 3 * _PAD)

    caption_w = paint._text_width(node.caption, _LABEL_SIZE, paint._SANS_EM) if node.caption else 0
    caption_x = _PAD
    if node.caption:
        if exit_x is not None and caption_x - 8 <= exit_x <= caption_x + caption_w + 8:
            caption_x = exit_x + 12  # the edge that leaves the frame must not cross the text
        width = max(width, caption_x + caption_w + _PAD)
    height = oy + inner.height + _PAD + (_CAPTION_ROW if node.caption else 0)
    if exit_name is not None:
        height = max(height, exit_bottom + _PAD + (_CAPTION_ROW if node.caption else 0))

    shifted = _shift_level(inner, ox, oy)
    feedback = (
        _feedback(shifted, node, exit_x or 0.0, exit_bottom, ox + inner.width)
        if loop_back
        else None
    )
    return Body(
        node=node,
        level=shifted,
        width=width,
        height=height,
        tag=tag,
        caption_x=caption_x,
        exit_x=exit_x,
        exit_bottom=exit_bottom,
        feedback=feedback,
    )


def _kind_of(level: Level, name: str) -> str:
    for n in level.nodes:
        if n.name == name:
            return n.kind
    return ""


def _shift_level(level: Level, dx: float, dy: float) -> Level:
    """The same level with every coordinate moved by ``(dx, dy)``. Nested frames keep their own."""
    placed = {
        name: replace(p, x=p.x + dx, y=p.y + dy)  # pyright: ignore[reportUnknownMemberType]
        for name, p in level.placed.items()
    }
    routes = tuple(
        replace(r, points=tuple((x + dx, y + dy) for x, y in r.points)) for r in level.routes
    )
    return replace(level, placed=placed, routes=routes)


def _feedback(
    level: Level,
    node: RNode,
    exit_x: float,
    exit_bottom: float,
    right: float,
) -> Feedback:
    """Points of the dashed edge: out of the exit stub, right into the gutter, up, then left."""
    terminal = level.placed[node.feedback]
    y0 = exit_bottom + 14
    gutter_x = right + 14
    ty = terminal.y
    tip_x = terminal.x + terminal.width / 2
    arrow = paint._arrow_size(level.look.style)[0]
    points = ((exit_x, y0), (gutter_x, y0), (gutter_x, ty), (tip_x + arrow, ty))
    label_w = paint._text_width("next round", _LABEL_SIZE, paint._SANS_EM)
    mid = (y0 + ty) / 2
    fits = (y0 - ty) >= label_w + 8
    return Feedback(points, (gutter_x + 12, mid) if fits else None, (tip_x, ty))


# ── Painting ─────────────────────────────────────────────────────────────────


def paint_level(
    parent: ET.Element,
    level: Level,
    *,
    dx: float = 0.0,
    dy: float = 0.0,
    depth: int = 0,
    flow: frozenset[str] = frozenset(),
    holder: str = "",
) -> None:
    """Paint a level: the frame boxes, then the edges, then the nodes, then the frames' insides."""
    for name, body in level.bodies.items():
        _paint_frame_box(
            parent, body, _moved(level.placed[name], dx, dy), level.look, depth, holder
        )
    for route in level.routes:
        moved = replace(route, points=tuple((x + dx, y + dy) for x, y in route.points))
        paint.paint_route(parent, moved, level.look, flow=route.target in flow)
    for n in level.nodes:
        if n.name in level.bodies:
            continue
        placed = _moved(level.placed[n.name], dx, dy)
        paint.paint_node(parent, n, placed, level.measured[n.name], _node_look(n, level.look))
        if holder:
            parent[-1].set("data-parent", paint._clean(holder))
    for name, body in level.bodies.items():
        _paint_frame_inside(parent, body, _moved(level.placed[name], dx, dy), depth + 1)


def _moved(p: lay.Placed, dx: float, dy: float) -> lay.Placed:
    return replace(p, x=p.x + dx, y=p.y + dy)


def _paint_frame_box(
    parent: ET.Element, body: Body, p: lay.Placed, look: Look, depth: int, holder: str
) -> None:
    node = body.node
    x, y = p.x - p.width / 2, p.y - p.height / 2
    state = "none" if look.stateless or node.state == "NONE" else node.state.lower()
    classes = [
        "dag-item",
        "dag-item--step",
        "dag-item--frame",
        "dag-frame",
        f"dag-frame--{node.shape}",
        f"is-{node.state.lower()}",
    ]
    attrs = {
        "class": " ".join(classes),
        "data-state": state,
        "data-kind": "frame",
        "data-shape": node.shape,
        "data-name": paint._clean(node.name),
        "data-depth": str(depth + 1),
    }
    if holder:
        attrs["data-parent"] = paint._clean(holder)
    group = ET.SubElement(parent, "g", attrs)
    ET.SubElement(group, "title").text = _frame_tooltip(node, look)
    r = paint.radii_for(look).box
    dash = "3 3" if node.shape == "loop" else "8 4"
    paint._rect(
        group,
        x,
        y,
        p.width,
        p.height,
        r,
        "dag-frame__border",
        **{"stroke-width": "1.5", "stroke-dasharray": dash},
    )
    paint._text(
        group,
        body.tag,
        x + 12,
        y + _TAG_ROW / 2,
        size=11,
        cls="dag-frame__tag",
        bold=True,
        mono=True,
    )
    word = paint._state_word(node, look)
    if word and node.state != "NONE":
        paint._text(
            group,
            word,
            x + p.width - 12,
            y + _TAG_ROW / 2,
            size=11,
            cls="dag-text--state",
            anchor="end",
        )
    if node.caption:
        paint._text(
            group,
            node.caption,
            x + body.caption_x,
            y + p.height - _CAPTION_ROW / 2 - 2,
            size=_LABEL_SIZE,
            cls="dag-frame__caption",
        )


def _frame_tooltip(node: RNode, look: Look) -> str:
    lines = [paint.tooltip(node, look)]
    if node.caption:
        lines.append(node.caption)
    return paint._clean("\n".join(lines))


def _paint_frame_inside(parent: ET.Element, body: Body, p: lay.Placed, depth: int) -> None:
    """The stub under the last step, the edge back of a loop, and the body's own level."""
    x, y = p.x - p.width / 2, p.y - p.height / 2
    group = ET.SubElement(
        parent,
        "g",
        {"class": "dag-frame-body", "data-parent": paint._clean(body.node.name)},
    )
    if body.exit_x is not None:
        ET.SubElement(
            group,
            "line",
            {
                "x1": paint._num(x + body.exit_x),
                "y1": paint._num(y + body.exit_bottom),
                "x2": paint._num(x + body.exit_x),
                "y2": paint._num(y + p.height),
                "class": "dag-edge",
                "stroke-width": "1.5",
            },
        )
    paint_level(
        group,
        body.level,
        dx=x,
        dy=y,
        depth=depth,
        holder=body.node.name,
    )
    if body.feedback is not None:
        _paint_feedback(group, body.feedback, x, y, body.level.look)


def _paint_feedback(
    parent: ET.Element, feedback: Feedback, dx: float, dy: float, look: Look
) -> None:
    points = [(px + dx, py + dy) for px, py in feedback.points]
    parts = [f"M{paint._num(points[0][0])} {paint._num(points[0][1])}"]
    radius = 26.0 if look.style == "band" else 14.0  # a corner is a curve, never a hook
    parts.extend(
        paint._quarter(points[i - 1], points[i], points[i + 1], radius)
        for i in range(1, len(points) - 1)
    )
    parts.append(f"L{paint._num(points[-1][0])} {paint._num(points[-1][1])}")
    path = ET.SubElement(
        parent,
        "path",
        {
            "d": " ".join(parts),
            "class": "dag-edge dag-edge--feedback",
            "stroke-width": "1.5",
            "stroke-dasharray": "5,3",
            "stroke-linejoin": "round",
        },
    )
    ET.SubElement(path, "title").text = "next round: the result becomes the state of the next round"
    arrow, half = paint._arrow_size(look.style)
    tip_x, tip_y = feedback.arrive[0] + dx, feedback.arrive[1] + dy
    ET.SubElement(
        parent,
        "polygon",
        {
            "points": (
                f"{paint._num(tip_x + arrow)},{paint._num(tip_y - half)} "
                f"{paint._num(tip_x + arrow)},{paint._num(tip_y + half)} "
                f"{paint._num(tip_x)},{paint._num(tip_y)}"
            ),
            "class": "dag-arrow dag-arrow--feedback",
        },
    )
    if feedback.label_at is not None:
        lx, ly = feedback.label_at[0] + dx, feedback.label_at[1] + dy
        paint._text(
            parent,
            "next round",
            lx,
            ly,
            size=_LABEL_SIZE,
            cls="dag-frame__caption",
            anchor="middle",
        )
        parent[-1].set("transform", f"rotate(-90 {paint._num(lx)} {paint._num(ly)})")


# ── The whole graph ──────────────────────────────────────────────────────────


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
    """Return the SVG text of a graph that has frames."""
    children: dict[str, list[RNode]] = {}
    for n in nodes:
        children.setdefault(n.parent, []).append(n)
    ctx = Ctx(children, {n.name: n.parent for n in nodes}, edges, look)
    has_output = bool(output_label) and final is not None
    level = build_level(
        ctx,
        "",
        0,
        look,
        margin=lay.LayoutSettings().margin,
        output_label=output_label,
        final=final,
    )

    legend_states = paint._legend_states(level.nodes, look) if legend else []
    canvas_h = level.height + (paint._LEGEND_HEIGHT if legend_states else 0)
    canvas_w = max(
        level.width, paint._legend_width(legend_states) + 2 * lay.LayoutSettings().margin
    )
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
            "width": paint._num(canvas_w),
            "height": paint._num(canvas_h),
            "viewBox": f"0 0 {paint._num(canvas_w)} {paint._num(canvas_h)}",
        },
    )
    if css is not None:
        ET.SubElement(svg, "style").text = css
    if background:
        ET.SubElement(
            svg,
            "rect",
            {
                "class": "dag-canvas",
                "width": paint._num(canvas_w),
                "height": paint._num(canvas_h),
            },
        )
        if look.style == "blueprint":
            paint._path(svg, paint._grid(canvas_w, canvas_h), "dag-grid")
    pending = frozenset(
        n.name for n in level.nodes if n.state == "PENDING" and not look.stateless and animate
    )
    paint_level(svg, level, flow=pending)
    if has_output and lay.OUTPUT in level.placed:
        paint.paint_output(svg, output_label, level.placed[lay.OUTPUT], look)
    if legend_states:
        paint._paint_legend(svg, legend_states, level.height + paint._LEGEND_HEIGHT / 2)
    return ET.tostring(svg, encoding="unicode")
