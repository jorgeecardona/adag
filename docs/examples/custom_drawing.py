"""Change what one node shows, then draw the graph."""

from dataclasses import dataclass, replace

from pydantic import BaseModel

from adag import InputParam, Step, node_metadata, render_graph_svg, walk


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class Square(Step[Number]):
    def run(self, x: int) -> Number:
        return Number(value=x * x)


nodes, edges = walk(Square()(x=InputParam[int]("x")))
metadata = [
    replace(m, title="Square the input", description="x * x") if m.name == "Square" else m
    for m in node_metadata(nodes)
]
svg = render_graph_svg(metadata, [(edge.source, edge.target) for edge in edges])
print([(m.name, m.kind, m.state) for m in metadata])
print(type(svg).__name__, "Square the input" in svg)
