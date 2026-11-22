"""A branch of a connection is a square corner with a dot, so it does not look like an accident."""

from tests.graphs_fixtures import all_graphs

from adag import svg_layout as lay
from adag import svg_paint as paint
from adag.visualization import SvgOptions, render_graph_svg


def _route(source: str, target: str, *points: lay.Point) -> lay.Route:
    return lay.Route(source=source, target=target, points=points)


def test_edge_leaving_a_shared_drop_is_a_branch() -> None:
    straight = _route("a", "b", (50, 0), (50, 100))
    turning = _route("a", "c", (50, 0), (50, 40), (90, 40), (90, 100))
    assert paint.branch_points([straight, turning]) == [frozenset(), frozenset({(50.0, 40.0)})]


def test_edge_joining_a_shared_drop_is_a_branch() -> None:
    trunk = _route("a", "b", (50, 0), (50, 100))
    joining = _route("a", "c", (30, 0), (30, 20), (50, 20), (50, 60))
    assert paint.branch_points([trunk, joining])[1] == frozenset({(50.0, 20.0)})


def test_edges_of_different_sources_never_branch() -> None:
    one = _route("a", "c", (50, 0), (50, 100))
    two = _route("b", "d", (50, 0), (50, 40), (90, 40), (90, 100))
    assert paint.branch_points([one, two]) == [frozenset(), frozenset()]


def test_blueprint_draws_junction_dots_and_other_styles_do_not() -> None:
    graph = all_graphs()["ml_training"]
    blueprint = render_graph_svg(list(graph.nodes), list(graph.edges), options=SvgOptions())
    band = render_graph_svg(list(graph.nodes), list(graph.edges), options=SvgOptions(style="band"))
    assert 'class="dag-junction"' in blueprint
    assert 'class="dag-junction"' not in band
