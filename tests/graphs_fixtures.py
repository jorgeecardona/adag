"""Graphs the SVG tests and the golden images draw.

To cover a new drawing bug, add a graph here (a name, nodes, edges) and run
``ADAG_UPDATE_GOLDEN=1 pytest tests/test_golden_graphs.py`` to write its image. Every graph is
drawn in every style, input mode and density by ``tests/test_svg_layout.py``.

The two ``namecheck`` graphs are the shape of a real consumer's pipeline (a domain-name checker):
four inputs, a name that fans out to many steps, one step with eight inputs and edges that skip
levels. They are copies of the structure, not imports of the consumer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from adag.visualization import NodeMetadata

Edges = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class GraphFixture:
    """One graph: its nodes, its edges and the step that feeds the output box."""

    name: str
    nodes: tuple[NodeMetadata, ...]
    edges: Edges
    final: str | None = None
    output_label: str = ""
    note: str = ""


def _key(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:12]


def step(
    name: str,
    title: str,
    description: str = "",
    state: str = "PENDING",
    params: tuple[tuple[str, str], ...] = (),
    shape: str = "",
) -> NodeMetadata:
    """A step with a fixed key, so the drawing is the same on every run."""
    key = "" if state == "BLOCKED" else _key(name)
    return NodeMetadata(
        name=name,
        kind="step",
        title=title,
        description=description,
        params=params,
        state=state,
        shape=shape,
        key=key,
    )


def param(name: str, value: str = "") -> NodeMetadata:
    """An ``InputParam`` node with an optional default."""
    return NodeMetadata(name=name, kind="param", title=name, state="SOURCE", value=value)


def file_input(name: str, path: str) -> NodeMetadata:
    """A ``FileSource`` node."""
    return NodeMetadata(name=name, kind="file", title=name, state="SOURCE", value=path)


def _chain3() -> GraphFixture:
    return GraphFixture(
        "chain3",
        (
            step("load", "Load", "Read the CSV file.", "CACHED"),
            step("clean", "Clean", "Normalize the text.", "CACHED"),
            step("report", "Report", "Write the summary."),
        ),
        (("load", "clean"), ("clean", "report")),
        final="report",
        output_label="Summary",
        note="Three steps in a line.",
    )


def _diamond_nodes() -> tuple[NodeMetadata, ...]:
    return (
        step("load", "Load", "Read the CSV file.", "CACHED"),
        step("clean", "Clean", "Normalize the text.", "PENDING"),
        step("count", "Count", "Count the rows.", "CACHED"),
        step("report", "Report", "Write the summary.", "PENDING"),
    )


_DIAMOND_EDGES: Edges = (
    ("load", "clean"),
    ("load", "count"),
    ("clean", "report"),
    ("count", "report"),
)


def _diamond() -> GraphFixture:
    return GraphFixture(
        "diamond", _diamond_nodes(), _DIAMOND_EDGES, "report", "report.md", "Two paths that join."
    )


def _diamond_inputs() -> GraphFixture:
    return GraphFixture(
        "diamond_inputs",
        (param("path", "'data.csv'"), param("case", "'lower'"), *_diamond_nodes()),
        (("path", "load"), ("case", "clean"), *_DIAMOND_EDGES),
        "report",
        "report.md",
        "The diamond with two inputs.",
    )


def _fan_out_input() -> GraphFixture:
    return GraphFixture(
        "fan_out_input",
        (
            param("threshold", "0.5"),
            step("a", "Score A", "First scorer.", "CACHED"),
            step("b", "Score B", "Second scorer.", "PENDING"),
            step("c", "Score C", "Third scorer.", "PENDING"),
            step("pick", "Pick", "Choose the best score.", "PENDING"),
        ),
        (
            ("threshold", "a"),
            ("threshold", "b"),
            ("threshold", "c"),
            ("a", "pick"),
            ("b", "pick"),
            ("c", "pick"),
        ),
        "pick",
        "Choice",
        "One input read by three steps.",
    )


def _mapped() -> GraphFixture:
    return GraphFixture(
        "mapped",
        (
            step("docs", "List documents", "One record per file.", "CACHED"),
            step(
                "embed",
                "Embed each document",
                "One branch per item.",
                "PENDING",
                params=(("each item", "Tokenize > Encode"),),
                shape="mapped",
            ),
            step("index", "Build index", "Collect the vectors.", "PENDING"),
        ),
        (("docs", "embed"), ("embed", "index")),
        "index",
        "Index",
        "A fan-out node.",
    )


def _loop() -> GraphFixture:
    return GraphFixture(
        "loop",
        (
            param("x", "2.0"),
            step(
                "newton",
                "Newton",
                "Repeat until the estimate stops changing.",
                "PENDING",
                params=(("each round", "Refine"),),
                shape="loop",
            ),
            step("report", "Report", "Print the root.", "PENDING"),
        ),
        (("x", "newton"), ("newton", "report")),
        "report",
        "Root",
        "A loop node.",
    )


def _blocked() -> GraphFixture:
    return GraphFixture(
        "blocked",
        (
            file_input("sales.csv", "data/sales.csv"),
            step("load", "Load", "The file is missing.", "BLOCKED"),
            step("sum", "Sum", "Add the amounts.", "PENDING"),
        ),
        (("sales.csv", "load"), ("load", "sum")),
        "sum",
        "Total",
        "A step with no key.",
    )


def _long_edge() -> GraphFixture:
    return GraphFixture(
        "long_edge",
        (
            step("a", "Start", "First step.", "CACHED"),
            step("b", "Middle one", "Second step.", "CACHED"),
            step("c", "Middle two", "Third step.", "PENDING"),
            step("d", "Finish", "Uses the first and the third.", "PENDING"),
        ),
        (("a", "b"), ("b", "c"), ("c", "d"), ("a", "d")),
        "d",
        "Done",
        "An edge that skips two levels.",
    )


_P = (("as_of", "2026-11-22"),)


def _namecheck_nodes() -> list[NodeMetadata]:
    return [
        param("name"),
        param("tlds", "['com', 'sh']"),
        param("variants"),
        param("with_lookalikes", "True"),
        step(
            "controls",
            "Controls",
            "Control-test each registry: a registered domain reads TAKEN, a random one FREE.",
            "CACHED",
            _P,
        ),
        step(
            "pypi_exact",
            "Pypi Exact",
            "Is `pip install NAME` free to publish? One cached item.",
            "CACHED",
            _P,
        ),
        step("pypi_free", "Pypi Free", "The gate: the name goes on only if it is free on PyPI."),
        step(
            "domain_probes",
            "Domain Probes",
            "Base name, prefixes and suffixes on each TLD. Needs the controls, and a free PyPI name.",
            "PENDING",
            _P,
        ),
        step(
            "lookalikes",
            "Lookalikes",
            "Sound-alike and typo names on PyPI (crake -> crack). Taken ones are a trust risk.",
            "PENDING",
            _P,
        ),
        step(
            "decide",
            "Decide",
            "Viable = free on PyPI and at least one free .com (any prefix or suffix).",
        ),
    ]


_NAMECHECK_EDGES: Edges = (
    ("tlds", "controls"),
    ("name", "pypi_exact"),
    ("pypi_exact", "pypi_free"),
    ("pypi_free", "domain_probes"),
    ("tlds", "domain_probes"),
    ("variants", "domain_probes"),
    ("controls", "domain_probes"),
    ("pypi_free", "lookalikes"),
    ("with_lookalikes", "lookalikes"),
    ("name", "decide"),
    ("tlds", "decide"),
    ("variants", "decide"),
    ("pypi_exact", "decide"),
    ("domain_probes", "decide"),
    ("lookalikes", "decide"),
)


def _namecheck() -> GraphFixture:
    return GraphFixture(
        "namecheck",
        tuple(_namecheck_nodes()),
        _NAMECHECK_EDGES,
        "decide",
        "Verdict",
        "How the check runs: 4 inputs, 6 steps, edges that skip levels.",
    )


def _namecheck_big() -> GraphFixture:
    nodes = _namecheck_nodes()
    nodes += [
        step(
            "pronounce",
            "Pronounce",
            "How the name sounds in English, and how a Spanish speaker says it.",
            "PENDING",
        ),
        step(
            "connotations",
            "Connotations",
            "Insults, vulgar words and slang that the name sounds like.",
            "PENDING",
        ),
    ]
    edges = (
        *_NAMECHECK_EDGES,
        ("name", "pronounce"),
        ("name", "connotations"),
        ("name", "domain_probes"),
        ("pronounce", "decide"),
        ("connotations", "decide"),
    )
    return GraphFixture(
        "namecheck_big",
        tuple(nodes),
        edges,
        "decide",
        "Verdict",
        "The bigger run: one input feeds five steps, one step has eight inputs.",
    )


def all_graphs() -> dict[str, GraphFixture]:
    """Every fixture by name."""
    graphs = [
        _chain3(),
        _diamond(),
        _diamond_inputs(),
        _fan_out_input(),
        _mapped(),
        _loop(),
        _blocked(),
        _long_edge(),
        _namecheck(),
        _namecheck_big(),
    ]
    return {g.name: g for g in graphs}


GRAPHS: dict[str, GraphFixture] = all_graphs()
