"""``LNode.arrive_at`` and ``LNode.depart_at``: a node says where its edges end and leave."""

from adag import svg_layout as lay


def _box(name: str, width: float = 120, height: float = 40, **options: object) -> lay.LNode:
    return lay.LNode(name, width, height, **options)  # type: ignore[arg-type]


def _route(layout: lay.Layout, source: str, target: str) -> lay.Route:
    return next(r for r in layout.routes if (r.source, r.target) == (source, target))


def test_an_edge_ends_at_the_x_its_target_asked_for() -> None:
    frame = _box("F", 300, 200, arrive_at=(("A", -90.0), ("B", 90.0)))
    layout = lay.layout([_box("A"), _box("B"), frame], [("A", "F"), ("B", "F")])
    centre = layout.nodes["F"].x
    top = layout.nodes["F"].y - layout.nodes["F"].height / 2
    for source, offset in (("A", -90.0), ("B", 90.0)):
        end = _route(layout, source, "F").points[-1]
        assert abs(end[0] - (centre + offset)) < 0.01
        assert abs(end[1] - top) < 0.01


def test_a_node_with_asked_ports_takes_no_edge_on_its_side() -> None:
    # A and B stand far to the left. Without arrive_at the edges would enter the left face.
    frame = _box("F", 300, 200, arrive_at=(("A", 100.0),))
    others = [_box(n, 80) for n in ("A", "B", "C", "D")]
    edges = [("A", "F"), ("B", "X"), ("C", "X"), ("D", "X")]
    layout = lay.layout([*others, _box("X"), frame], edges)
    points = _route(layout, "A", "F").points
    assert abs(points[-1][0] - (layout.nodes["F"].x + 100.0)) < 0.01
    assert points[-1][1] == points[-2][1] or points[-1][0] == points[-2][0]
    assert points[-1][0] == points[-2][0]  # the last stretch is vertical: it ends on top


def test_an_edge_without_an_asked_port_still_finds_a_free_place() -> None:
    frame = _box("F", 300, 200, arrive_at=(("A", 0.0),))
    layout = lay.layout([_box("A"), _box("B"), frame], [("A", "F"), ("B", "F")])
    asked = _route(layout, "A", "F").points[-1][0]
    other = _route(layout, "B", "F").points[-1][0]
    assert abs(asked - layout.nodes["F"].x) < 0.01
    assert abs(other - asked) >= lay.SNAP_SPACING - 0.01


def test_every_edge_leaves_a_node_at_its_exit_x() -> None:
    frame = _box("F", 300, 200, depart_at=40.0)
    layout = lay.layout(
        [frame, _box("A"), _box("B"), _box("C")], [("F", "A"), ("F", "B"), ("F", "C")]
    )
    centre = layout.nodes["F"].x
    for target in ("A", "B", "C"):
        start = _route(layout, "F", target).points[0]
        assert abs(start[0] - (centre + 40.0)) < 0.01
        assert start[1] == layout.nodes["F"].y + layout.nodes["F"].height / 2


def test_without_asked_ports_nothing_changes() -> None:
    nodes = [_box("A"), _box("B"), _box("C")]
    edges = [("A", "C"), ("B", "C")]
    assert lay.layout(nodes, edges) == lay.layout(
        [_box("A", arrive_at=()), _box("B", depart_at=None), _box("C")], edges
    )


def test_a_straight_drop_still_works_into_a_node_that_asked_for_its_port() -> None:
    frame = _box("F", 200, 100, arrive_at=(("A", 0.0),))
    layout = lay.layout([_box("A"), frame], [("A", "F")])
    points = _route(layout, "A", "F").points
    assert len(points) == 2
    assert points[0][0] == points[1][0]
