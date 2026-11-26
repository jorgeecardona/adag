"""DAG visualization: walk the Deferred graph and render it as SVG.

tokens = tokenizer(text=FileSource("data.txt"))
embeddings = encoder(tokens=tokens)

dag(embeddings)                  # returns an SVG object; str() gives the text
dag(embeddings, path="out.svg")  # saves to a file
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import structlog

from adag.deferred import Deferred, InputParam
from adag.hashing import Placeholder
from adag.io import FileSource, atomic_write_text
from adag.key import Expansion
from adag.step import Step, StepResult
from adag.svg_frame import draw as _draw_framed
from adag.svg_paint import CORNER_SHAPES as _CORNER_SHAPES
from adag.svg_paint import DENSITIES as _DENSITIES
from adag.svg_paint import INPUTS as _INPUTS
from adag.svg_paint import STYLES as _STYLES
from adag.svg_paint import Corners, Density, Inputs, Style
from adag.svg_paint import Look as _Look
from adag.svg_paint import RNode as _RNode
from adag.svg_paint import draw as _draw_graph
from adag.svg_paint import wrap_text_lines as _wrap_text
from adag.svg_theme import stylesheet as _stylesheet

logger = structlog.get_logger()


class SVG:
    """Wrapper around an SVG string that Jupyter renders as an image."""

    def __init__(self, source: str) -> None:
        self.source = source

    def _repr_svg_(self) -> str:
        return self.source

    def __str__(self) -> str:
        return self.source


# ── Graph model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Node:
    """A node in the walked graph.

    `name` is the node's unique KEY (edges reference it, layout positions by it). `title`, when set, is the
    human display label; empty → the renderer derives one from `name`. A Step may expose a per-INSTANCE label
    via a `node_label` attribute (walk() then uses it as both name and title; the name gets a suffix on collision) so
    two instances of the SAME Step class render as two distinct boxes instead of collapsing into one.
    """

    name: str
    step: Step[Any] | None
    kind: str
    title: str = ""
    result: StepResult[Any] | None = None  # the graph node; its cache key covers the inputs
    value: str = ""
    """For an input node: its default value, or the file path. Empty when it has none."""
    parent: str = ""
    """The name of the frame node that holds this node, empty for a node at the top level."""


@dataclass(frozen=True)
class Edge:
    """A directed edge in the walked graph."""

    source: str
    target: str
    label: str


@dataclass(frozen=True)
class SvgOptions:
    """What the renderer emits.

    ``style`` is ``"blueprint"`` (mono type, rounded corners, right-angle edges), ``"band"`` (a
    header strip with the state word, curved edges) or ``"classic"`` (the look of adag 0.3.22).
    ``inputs`` says where ``InputParam`` and file inputs go: ``"terminals"`` (small nodes with an
    edge, all in the top row), ``"tabs"`` (a tab on the step) or ``"rows"`` (a line in the step).
    ``classic`` is the drawing of 0.3.22: its colours, box shapes, layout and edge curves. It has no
    input modes, densities or corner shapes, and draws inputs as ellipses and boxes where it put
    them.
    ``density="compact"`` draws only the title of each step. With ``tabs`` or ``rows`` the inputs
    are labels to its left.

    ``corners`` is ``"round"`` (the default) or ``"sharp"`` (boxes and bends almost square). It
    applies to ``blueprint`` and ``band``; ``classic`` ignores it.

    ``theme`` is ``"light"``, ``"dark"`` or ``"auto"`` (follow the browser's colour scheme).
    ``legend=True`` adds a row under the graph that names the state colours in use. ``animate=True``
    moves dashes along the edges into pending steps, unless the viewer asks for reduced motion.
    ``frames=True`` (the default) draws the body of a fan-out or a loop once, inside a dashed
    frame around it (``blueprint`` and ``band``). ``frames=False`` and ``classic`` draw such a
    node as one dashed box.

    ``include_styles=False`` leaves the SVG unpainted apart from its classes,
    for a host that supplies the whole stylesheet. ``show_params=False`` drops
    the per-step parameter lines, which also shrinks the boxes: the layout is
    computed from the same node data that gets drawn.
    """

    theme: str = "light"
    include_styles: bool = True
    include_background: bool = True
    show_params: bool = True
    style: Style = "blueprint"
    inputs: Inputs = "terminals"
    density: Density = "normal"
    corners: Corners = "round"
    legend: bool = False
    animate: bool = False
    frames: bool = True

    def __post_init__(self) -> None:
        for name, value, allowed in (
            ("style", self.style, _STYLES),
            ("inputs", self.inputs, _INPUTS),
            ("density", self.density, _DENSITIES),
            ("corners", self.corners, _CORNER_SHAPES),
        ):
            if value not in allowed:
                raise ValueError(f"SvgOptions.{name} must be one of {allowed}, not {value!r}")
        if self.theme not in ("light", "dark", "auto"):
            warnings.warn(
                f"SvgOptions.theme {self.theme!r} is not light, dark or auto: drawing it light",
                stacklevel=3,
            )

    def stylesheet(self) -> str:
        return _stylesheet(self.theme, animate=self.animate)


_STYLESHEET_LIGHT = _stylesheet("light")
_STYLESHEET_DARK = _stylesheet("dark")
_DEFAULT_SVG_OPTIONS = SvgOptions()


@dataclass(frozen=True)
class NodeMetadata:
    """Pre-extracted display info for a graph node, decoupled from Step introspection."""

    name: str
    kind: str
    title: str
    description: str = ""
    params: tuple[tuple[str, str], ...] = ()
    state: str = "PENDING"
    shape: str = ""
    """``mapped`` or ``loop`` for a node that builds its sub-graph at run time, else empty."""
    key: str = ""
    """The 12-hex cache key of a step, empty for a step that is blocked or has no key."""
    value: str = ""
    """For an input: its default value or file path, empty when it has none."""
    parent: str = ""
    """The name of the frame node that holds this node. A frame is a node that another node names
    as its ``parent``. Empty for a node at the top level."""
    caption: str = ""
    """For a frame: the sentence drawn under its body."""
    feedback: str = ""
    """For a loop frame: the name of the inner ``template_input`` that gets the next round's state."""


def _node_name_and_kind(deferred: Deferred[Any]) -> tuple[str, str] | None:
    """Return (name, kind) for a leaf Deferred, or None for StepResult."""
    if isinstance(deferred, FileSource):
        return Path(deferred.path).name, "file"
    if isinstance(deferred, InputParam):
        return deferred.name, "param"
    return None


_MAX_VALUE = 40


def _leaf_value(deferred: Deferred[Any]) -> str:
    """What an input node shows besides its name: the default of a param, the path of a file."""
    if isinstance(deferred, FileSource):
        return str(deferred.path)
    if isinstance(deferred, InputParam) and deferred.has_default:
        text = repr(deferred.default)
        return text if len(text) <= _MAX_VALUE else text[: _MAX_VALUE - 3] + "..."
    return ""


_MAX_FRAME_DEPTH = 3
"""A frame inside a frame inside a frame is drawn. A fourth level is one dashed box."""

TEMPLATE_INPUT = "template_input"
"""The node kind of an input of a template, drawn inside its frame."""


def _placeholders(value: Any) -> list[Placeholder]:
    """The placeholders in a field or an input value, also inside a list, tuple or dict."""
    found: list[Placeholder] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is Placeholder:  # never ==, in or hash: a placeholder refuses them all
            found.append(item)
        elif isinstance(item, (list, tuple)):
            stack.extend(cast("list[Any] | tuple[Any, ...]", item))
        elif isinstance(item, dict):
            mapping = cast("dict[Any, Any]", item)
            stack.extend(mapping.keys())
            stack.extend(mapping.values())
    return found


def _results(root: StepResult[Any]) -> dict[int, StepResult[Any]]:
    """Every ``StepResult`` reachable from ``root`` by the ``id`` of its step."""
    found: dict[int, StepResult[Any]] = {}
    todo: list[Any] = [root]
    while todo:
        item = todo.pop()
        if isinstance(item, StepResult):
            result = cast("StepResult[Any]", item)
            step: Any = result.step
            if id(step) not in found:
                found[id(step)] = result
                todo.extend(result.sources.values())
    return found


class _Walker:
    """Walks one level of the graph. A frame's body is walked by a ``_Walker`` of its own.

    All walkers of one ``walk`` share ``nodes`` and ``edges``. A walker knows its ``prefix`` (the
    frame name and a slash), its ``parent`` frame and the steps it has placed.
    """

    def __init__(
        self,
        nodes: dict[str, Node],
        edges: list[Edge],
        *,
        prefix: str = "",
        parent: str = "",
        outer: _Walker | None = None,
        captured: frozenset[int] = frozenset(),
    ) -> None:
        self.nodes = nodes
        self.edges = edges
        self.prefix = prefix
        self.parent = parent
        self.outer = outer
        self.captured = captured
        self.placed: dict[int, str] = {}  # id(step) -> the unique node name of that instance

    def terminal(self, name: str, title: str = "", value: str = "") -> str:
        """The node for one input of the template, made once per name."""
        full = f"{self.prefix}{name}"
        if full not in self.nodes:
            self.nodes[full] = Node(
                name=full,
                step=None,
                kind=TEMPLATE_INPUT,
                title=title or name,
                value=value,
                parent=self.parent,
            )
        return full

    def visit(self, deferred: Any) -> str:
        leaf = _node_name_and_kind(deferred)
        if leaf is not None:
            name = f"{self.prefix}{leaf[0]}"
            if name not in self.nodes:
                self.nodes[name] = Node(
                    name=name,
                    step=None,
                    kind=leaf[1],
                    value=_leaf_value(deferred),
                    parent=self.parent,
                )
            return name

        # Capture .step while deferred is still Any (before isinstance narrows it).
        step: Any = deferred.step if hasattr(deferred, "step") else None

        if isinstance(deferred, StepResult):
            if id(step) in self.captured and self.outer is not None:
                return self._enter_from_outside(cast("StepResult[Any]", deferred))
            if id(step) in self.placed:  # the same instance reached again (shared upstream)
                return self.placed[id(step)]
            label = getattr(step, "node_label", "") or ""
            base = f"{self.prefix}{label or type(step).__name__}"
            name = base
            suffix = 2
            while name in self.nodes:  # a DIFFERENT instance wants the same name
                name = f"{base}#{suffix}"
                suffix += 1
            self.placed[id(step)] = name
            self.nodes[name] = Node(
                name=name,
                step=step,
                kind="step",
                title=label,
                result=cast("StepResult[Any]", deferred),
                parent=self.parent,
            )
            for input_name, source in deferred.sources.items():
                if isinstance(source, Deferred):
                    source_name = self.visit(source)
                    if source_name:
                        self.edges.append(Edge(source=source_name, target=name, label=input_name))
            if self.parent:
                self._take_template_inputs(name, step, deferred.sources)
            return name

        return ""

    def _take_template_inputs(self, name: str, step: Any, sources: dict[str, Any]) -> None:
        """An edge from each template input that this step takes, as a field or as an input."""
        uses: dict[str, str] = {}
        for field in dataclasses.fields(step):
            for placeholder in _placeholders(getattr(step, field.name)):
                uses.setdefault(placeholder.name, field.name)
        for input_name, source in sources.items():
            for placeholder in _placeholders(source):
                uses.setdefault(placeholder.name, input_name)
        for template_input, label in uses.items():
            edge = Edge(self.terminal(template_input), name, label)
            if edge not in self.edges:
                self.edges.append(edge)

    def _enter_from_outside(self, deferred: StepResult[Any]) -> str:
        """A step the template reaches through a closure lives outside: an input carries it in."""
        assert self.outer is not None
        outer_name = self.outer.visit(deferred)
        title = self.outer.nodes[outer_name].title or outer_name
        terminal = self.terminal(outer_name, title=title)
        edge = Edge(outer_name, terminal, "captured")
        if edge not in self.edges:
            self.edges.append(edge)
        return terminal


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= _MAX_VALUE else text[: _MAX_VALUE - 3] + "..."


def _expand(walker: _Walker, depth: int) -> None:
    """Add the body of every node of this level that has an expansion, as children of the node."""
    if depth >= _MAX_FRAME_DEPTH:
        return
    level = [n for n in walker.nodes.values() if n.parent == walker.parent and n.kind == "step"]
    for frame in level:
        expansion = frame.step.expansion() if frame.step is not None else None
        if expansion is not None:
            _expand_one(walker, frame, expansion, depth)


def _expand_one(walker: _Walker, frame: Node, expansion: Expansion, depth: int) -> None:
    try:
        first = expansion.template.trace()
        second = expansion.template.trace()
    except TypeError:  # includes TemplateInputError: the node stays one dashed box
        return
    # A step that both traces share (a closure) or that this level has already placed is outside.
    in_second = _results(second)
    captured = frozenset(
        step_id for step_id in _results(first) if step_id in in_second or step_id in walker.placed
    )
    body = _Walker(
        walker.nodes,
        walker.edges,
        prefix=f"{frame.name}/",
        parent=frame.name,
        outer=walker,
        captured=captured,
    )
    _bind(walker, body, frame, expansion)
    body.visit(first)
    _expand(body, depth + 1)


def _bind(walker: _Walker, body: _Walker, frame: Node, expansion: Expansion) -> None:
    """Make the inputs of the template, and point the edges that enter the frame at them."""
    for outer_input, template_input in expansion.binds:
        entering = [
            i
            for i, e in enumerate(walker.edges)
            if e.target == frame.name and e.label == outer_input
        ]
        if entering:
            terminal = body.terminal(template_input)
            for i in entering:
                old = walker.edges[i]
                walker.edges[i] = Edge(old.source, terminal, old.label)
        elif expansion.shape == "loop" and frame.step is not None:
            start = getattr(frame.step, outer_input, None)
            if start is not None:
                body.terminal(template_input, value=_short(start))
    if expansion.feedback:
        body.terminal(expansion.feedback)


def walk(output: Deferred[Any], *, expand: bool = False) -> tuple[list[Node], list[Edge]]:
    """Walk any Deferred backward and return the full dependency graph.

    Step nodes are identified by instance (``id(step)``), not class name. A shared upstream (the
    same instance reached on two paths) is one node. Two distinct instances of the same class are
    two nodes. Each node's label is its ``node_label`` attribute when present, else the class name.
    The key gets a suffix (``Name#2``) only when two instances would otherwise collide.

    With ``expand=True`` a node that builds its sub-graph at run time (a template fan-out or a
    loop) gets the steps of its template as children: nodes whose ``parent`` is that node, named
    ``"<node>/<step>"``, plus one ``template_input`` node for each input of the template. Edges
    that enter the node end at those inputs. A template that cannot be built over placeholders
    stays one node.
    """
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    top = _Walker(nodes, edges)
    top.visit(output)
    if expand:
        _expand(top, 0)
    return list(nodes.values()), edges


# ── Shared utilities ─────────────────────────────────────────────────────────


def compute_state(node: Node, cache_dir: Path | None) -> str:
    """Return the state a node is drawn with: ``CACHED``, ``PENDING``, ``BLOCKED`` or ``SOURCE``.

    ``SOURCE`` is an ``InputParam`` or a file. ``BLOCKED`` is a step with no cache key, for
    example a step that reads a missing file, and every step downstream of it. Without
    ``cache_dir`` every step is ``PENDING``.
    The key is computed with ``InputParam`` defaults, not with the values of a run.
    """
    if node.step is None:
        return "SOURCE"
    if cache_dir is None:
        return "PENDING"
    h = node.step.content_hash() if hasattr(node.step, "content_hash") else None
    if h is None or (node.result is not None and node.result.content_hash() is None):
        return "BLOCKED"
    if node.result is not None:
        # The pipeline caches under the node key (step and inputs), not the step hash.
        return "CACHED" if node.result.is_cached(cache_dir) else "PENDING"
    is_cached_attr: object = getattr(node.step, "is_cached", None)
    if is_cached_attr is not None:
        cached = is_cached_attr(cache_dir) if callable(is_cached_attr) else is_cached_attr
        return "CACHED" if cached else "PENDING"
    return "PENDING"


def _camel_to_words(name: str) -> str:
    title = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    title = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", title)
    return title.strip()


def _first_doc_line(step: Step[Any]) -> str:
    """First sentence of docstring, or label/description class attribute."""
    for cls in type(step).__mro__:
        if cls is Step or cls is object:
            break
        doc = getattr(cls, "__doc__", None)
        # ``dataclass`` fills a missing docstring with the signature, "Name(field: int = 1)".
        if doc and not doc.startswith(f"{cls.__name__}("):
            return doc.strip().split("\n")[0].strip().rstrip(".")
    return getattr(type(step), "description", "") or getattr(type(step), "label", "")


def _scalar_params(step: Step[Any], max_str_len: int = 30) -> list[tuple[str, str]]:
    """Extract scalar dataclass fields (int, float, bool, short str) for display."""
    items: list[tuple[str, str]] = []
    for f in dataclasses.fields(step):
        if (
            f.name == "node_label"
        ):  # the box's display label (see walk()), not a step parameter to list
            continue
        value = getattr(step, f.name)
        if isinstance(value, bool):
            items.append((f.name, str(value)))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            items.append((f.name, f"{value:g}" if isinstance(value, float) else str(value)))
        elif isinstance(value, str) and 0 < len(value) <= max_str_len:
            items.append((f.name, value))
        elif value is None:
            items.append((f.name, "None"))
    return items


_wrap_text_lines = _wrap_text  # the first-release private name, kept for old callers
LayoutNode = _RNode  # the old name of the node the painter draws, kept for old imports


# ── Entry point ──────────────────────────────────────────────────────────────


def dag(
    output: Deferred[Any],
    *,
    cache_dir: Path | None = None,
    path: str | Path | None = None,
    prefix: str = "",
    options: SvgOptions = _DEFAULT_SVG_OPTIONS,
) -> SVG:
    """Render the DAG as SVG.

    Args:
        output: Any Deferred: walks backward to discover the full graph.
        cache_dir: Cache directory (for cached/pending state coloring).
        path: Save SVG to this path.
        prefix: Text removed from the start of step names in titles.
        options: Theme, and whether to embed the stylesheet, paint a background
            or draw the per-step parameters.
    """
    graph_nodes, graph_edges = walk(output, expand=options.frames and options.style != "classic")
    return SVG(
        _render_svg(
            output,
            graph_nodes,
            graph_edges,
            cache_dir=cache_dir,
            prefix=prefix,
            path=path,
            options=options,
        )
    )


_NESTED_WIDTH = 56


def _nested_params(expansion: Expansion) -> tuple[tuple[str, str], ...]:
    """The steps of a node's template, upstream first, as one line inside the node.

    Empty when the template cannot be built over placeholders.
    """
    try:
        graph_nodes, _ = walk(expansion.template.trace())
    except TypeError:  # includes TemplateInputError
        return ()
    names = [n.title or n.name for n in reversed(graph_nodes) if n.kind == "step"]
    text = " > ".join(names)
    if len(text) > _NESTED_WIDTH:
        text = text[: _NESTED_WIDTH - 3] + "..."
    label = "each round" if expansion.shape == "loop" else "each item"
    return ((label, text),) if names else ()


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _children_keys(node: Node, cache_dir: Path) -> tuple[list[str], bool] | None:
    """The keys of the branches or rounds of a node, and whether they come from the last run.

    The key record of the last run lists them. Without a record, a fan-out over a dict gives
    its branches from the dict. ``None`` when neither is known.
    """
    key = _node_key(node)
    if key:
        try:
            record = json.loads(cache_dir.joinpath(".keys", f"{key}.json").read_text())
            children = record["expansion"]["children"]
            if isinstance(children, list):
                return [str(c) for c in cast("list[Any]", children)], True
        except (OSError, ValueError, KeyError, TypeError):
            pass
    step: Any = node.step
    over = cast("dict[Any, Any] | None", getattr(step, "over", None))
    if getattr(step, "template", None) is not None and isinstance(over, dict) and over:
        try:
            keys = [step._branch(k, v).content_hash() for k, v in over.items()]
        except (TypeError, ValueError):
            return None
        return [k for k in keys if k is not None], False
    return None


def _run_note(node: Node, expansion: Expansion, cache_dir: Path | None) -> str:
    """What the cache says about the branches or rounds: ``3 items, 2 cached``.

    A count that comes from the last run starts with ``last run:``. Empty when nothing is known.
    """
    if cache_dir is None:
        return ""
    found = _children_keys(node, cache_dir)
    if found is None:
        return ""
    keys, from_run = found
    word = "round" if expansion.shape == "loop" else "item"
    cached = sum(1 for k in keys if cache_dir.joinpath(f"{k}.done").exists())
    text = f"{_plural(len(keys), word)}, {cached} cached"
    return f"last run: {text}" if from_run else text


def _node_key(node: Node) -> str:
    """The cache key of a step node, empty when it has none (a blocked step)."""
    if node.result is None:
        return ""
    return node.result.content_hash() or ""


def _walk_to_metadata(
    graph_nodes: list[Node], *, cache_dir: Path | None, prefix: str
) -> dict[str, NodeMetadata]:
    """Convert walk results into NodeMetadata, doing Step introspection up front."""
    result: dict[str, NodeMetadata] = {}
    frames = {node.parent for node in graph_nodes if node.parent}
    for node in graph_nodes:
        caption = ""
        feedback = ""
        if node.kind == "step" and node.step is not None:
            plain = _tail(node.name) if node.parent else node.name
            title = node.title or _camel_to_words(plain.removeprefix(prefix) if prefix else plain)
            description = _first_doc_line(node.step)
            params = tuple(_scalar_params(node.step))
            shape = ""
            expansion = node.step.expansion()
            if expansion is not None:
                shape = expansion.shape
                description = expansion.note
                params = _nested_params(expansion)
                if node.name in frames:
                    caption = expansion.caption
                    note = "" if node.parent else _run_note(node, expansion, cache_dir)
                    if note and expansion.shape == "loop":
                        caption = f"{caption}; {note}"
                    elif note:
                        caption = note
                    feedback = f"{node.name}/{expansion.feedback}" if expansion.feedback else ""
        else:
            title = node.title or node.name
            description = ""
            params = ()
            shape = ""
        # A step inside a frame is one step of a template, not one run: it has no state and no key.
        in_body = bool(node.parent)
        state = "NONE" if in_body and node.kind == "step" else compute_state(node, cache_dir)
        result[node.name] = NodeMetadata(
            name=node.name,
            kind=node.kind,
            title=title,
            description=description,
            params=params,
            state=state,
            shape=shape,
            key="" if in_body else _node_key(node),
            value=node.value,
            parent=node.parent,
            caption=caption,
            feedback=feedback,
        )
    return result


def _tail(name: str) -> str:
    """The name of a step inside a frame, without the frame's name before the slash."""
    return name.rsplit("/", 1)[-1]


def node_metadata(
    nodes: list[Node], *, cache_dir: Path | None = None, prefix: str = ""
) -> list[NodeMetadata]:
    """Return the display data for each node from ``walk``, in the same order.

    Use it to draw a pipeline your own way: walk the graph, change the ``NodeMetadata`` you want
    (``dataclasses.replace`` a title, a description or the params), then pass the list to
    ``render_graph_svg``. ``dag`` does the same with no changes.

    Args:
        nodes: the nodes returned by ``walk``.
        cache_dir: colour each step by its cache state, as ``dag(..., cache_dir=)`` does.
        prefix: strip this from step names in the titles.
    """
    by_name = _walk_to_metadata(nodes, cache_dir=cache_dir, prefix=prefix)
    return [by_name[node.name] for node in nodes]


def _depth(node: NodeMetadata, by_name: dict[str, NodeMetadata]) -> int:
    """How many frames hold this node."""
    depth = 0
    while node.parent and node.parent in by_name:
        node = by_name[node.parent]
        depth += 1
    return depth


def _collapse(
    nodes: dict[str, NodeMetadata], edges: list[tuple[str, str]], depth: int = 0
) -> tuple[dict[str, NodeMetadata], list[tuple[str, str]]]:
    """Fold every node deeper than ``depth`` frames into the frame that holds it.

    The children are dropped. An edge that ends in a dropped node ends in the frame instead, and
    an edge that stays inside one frame is dropped. ``depth=0`` gives the graph of the top level,
    where a node with a template is one box.
    """
    if not any(n.parent for n in nodes.values()):
        return nodes, edges
    keep = {name: n for name, n in nodes.items() if _depth(n, nodes) <= depth}

    def home(name: str) -> str:
        while name not in keep:
            name = nodes[name].parent
        return name

    folded = [(home(a), home(b)) for a, b in edges if a in nodes and b in nodes]
    return keep, list(dict.fromkeys((a, b) for a, b in folded if a != b))


def _to_render_nodes(
    nodes: dict[str, NodeMetadata],
    edges: list[tuple[str, str]],
    final_step_name: str | None,
    options: SvgOptions,
) -> list[_RNode]:
    """Turn the metadata into what the painter draws. ``is_leaf`` is decided here, from every edge.

    An edge that enters a frame ends on an input inside it, and it feeds the frame as well.
    """

    def holders(name: str) -> list[str]:
        chain: list[str] = []
        while name in nodes and nodes[name].parent:
            name = nodes[name].parent
            chain.append(name)
        return chain

    fed: set[str] = set()
    for source, target in edges:
        fed.add(target)
        fed.update(set(holders(target)) - set(holders(source)) - {source})
    return [
        _RNode(
            name=meta.name,
            kind=meta.kind,
            title=meta.title,
            description=meta.description,
            params=meta.params if options.show_params else (),
            state=meta.state,
            shape=meta.shape,
            key=meta.key,
            value=meta.value,
            is_leaf=meta.name not in fed,
            is_final=meta.name == final_step_name,
            parent=meta.parent,
            caption=meta.caption,
            feedback=meta.feedback,
        )
        for meta in nodes.values()
    ]


def _draw(
    nodes: dict[str, NodeMetadata],
    edges: list[tuple[str, str]],
    *,
    output_label: str,
    final_step_name: str | None,
    options: SvgOptions,
    stateless: bool,
) -> str:
    # classic keeps the colours of 0.3.22, which paint a graph with no state as pending
    classic = options.style == "classic"
    # Without frames a node with a template is one dashed box: fold its children into it.
    framed = options.frames and not classic and any(n.parent for n in nodes.values())
    if not framed:
        nodes, edges = _collapse(nodes, edges)
    # classic is the drawing of 0.3.22: it has no input modes, densities or corner shapes
    look = _Look(
        options.style,
        "terminals" if classic else options.inputs,
        "normal" if classic else options.density,
        stateless and not classic,
        "round" if classic else options.corners,
    )
    return (_draw_framed if framed else _draw_graph)(
        _to_render_nodes(nodes, edges, final_step_name, options),
        edges,
        look=look,
        output_label=output_label,
        final=final_step_name,
        css=options.stylesheet() if options.include_styles else None,
        background=options.include_background,
        legend=options.legend,
        animate=options.animate,
    )


def _render_svg(
    output: Deferred[Any],
    graph_nodes: list[Node],
    graph_edges: list[Edge],
    *,
    cache_dir: Path | None = None,
    prefix: str = "",
    path: str | Path | None = None,
    options: SvgOptions = _DEFAULT_SVG_OPTIONS,
) -> str:
    """Render the DAG (walked from a Deferred output) as an SVG string."""
    nodes = _walk_to_metadata(graph_nodes, cache_dir=cache_dir, prefix=prefix)
    edges = [(e.source, e.target) for e in graph_edges]

    output_label = ""
    final_step_name: str | None = None
    if isinstance(output, StepResult):
        result_type = output.step.result_model()
        output_label = _camel_to_words(result_type.__name__) if result_type else "output"
        # The name walk() gave this instance: it differs from the class name for a node_label or a
        # "#2" suffix, and the output edge needs the node's real name.
        final_step_name = next((n.name for n in graph_nodes if n.step is output.step), None)

    svg_string = _draw(
        nodes,
        edges,
        output_label=output_label,
        final_step_name=final_step_name,
        options=options,
        stateless=cache_dir is None,
    )

    if path is not None:
        atomic_write_text(Path(str(path)), svg_string)
        logger.info("dag.svg.saved", path=str(path))

    return svg_string


def render_graph_svg(
    nodes: list[NodeMetadata],
    edges: list[tuple[str, str]],
    *,
    output_label: str = "",
    final_step_name: str | None = None,
    options: SvgOptions = _DEFAULT_SVG_OPTIONS,
) -> str:
    """Return the SVG for explicit node metadata and ``(source, target)`` edge pairs.

    Use it to draw a graph that is not a live pipeline, for example one built from your own
    records. ``dag(output)`` and ``render_pipeline_svg(pipeline_dir)`` call the same layout.
    A graph with no cached or blocked node has no state to show: its steps are drawn neutral.
    """
    # Params are dropped before layout, not at paint time: the box sizes come from the same data.
    stateless = not any(n.state in ("CACHED", "BLOCKED") for n in nodes)
    return _draw(
        {n.name: n for n in nodes},
        edges,
        output_label=output_label,
        final_step_name=final_step_name,
        options=options,
        stateless=stateless,
    )


_render_graph_svg = render_graph_svg  # the first-release private name, kept for old callers


def _humanize_step_name(name: str) -> str:
    """Strip a numeric ordering prefix and convert snake_case into title-cased words."""
    cleaned = re.sub(r"^\d+_", "", name)
    return cleaned.replace("_", " ").title()


def _scalar_params_from_spec(spec: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Extract scalar entries from a step spec dict for display in the SVG."""
    items: list[tuple[str, str]] = []
    for key, value in spec.items():
        if isinstance(value, bool):
            items.append((key, str(value)))
        elif isinstance(value, (int, float)):
            items.append((key, f"{value:g}" if isinstance(value, float) else str(value)))
        elif isinstance(value, str) and 0 < len(value) <= 30:
            items.append((key, value))
        elif value is None:
            items.append((key, "None"))
    return tuple(items)


def render_pipeline_svg(
    pipeline_dir: str | Path, *, options: SvgOptions = _DEFAULT_SVG_OPTIONS
) -> str | None:
    """Render the pipeline at ``pipeline_dir`` as SVG from its on-disk manifest.

    Reads ``pipeline.json`` and each step's ``step.json``, builds the graph,
    and produces an SVG string. Returns ``None`` when there is no manifest
    or the manifest declares no steps.
    """
    base = str(pipeline_dir)
    pipeline_path = os.path.join(base, "pipeline.json")
    if not os.path.exists(pipeline_path):
        return None
    with open(pipeline_path) as f:
        pipeline_data = json.load(f)

    step_dirs: list[str] = pipeline_data.get("steps", [])
    if not step_dirs:
        return None

    nodes: list[NodeMetadata] = []
    for step_dir in step_dirs:
        step_json_path = os.path.join(base, step_dir, "step.json")
        spec: dict[str, Any] = {}
        kind = ""
        if os.path.exists(step_json_path):
            with open(step_json_path) as f:
                step_data = json.load(f)
            spec = step_data.get("spec", {})
            kind = step_data.get("kind", "")

        nodes.append(
            NodeMetadata(
                name=step_dir,
                kind="step",
                title=_humanize_step_name(step_dir),
                description=kind,
                params=_scalar_params_from_spec(spec),
            )
        )

    edges: list[tuple[str, str]] = []
    for edge in pipeline_data.get("edges", []):
        source = edge.get("source", "")
        target = edge.get("target", "")
        if source.startswith("parameter:"):
            continue
        src_node = source.split(".", 1)[0]
        tgt_node = target.split(".", 1)[0]
        if src_node and tgt_node:
            edges.append((src_node, tgt_node))

    final_step_name = pipeline_data.get("output") or step_dirs[-1]
    return render_graph_svg(
        nodes, edges, output_label="", final_step_name=final_step_name, options=options
    )
