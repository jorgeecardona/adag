"""``Template``: a sub-graph with named inputs, built by a function.

A ``FanOutStep`` builds one sub-graph per item. A ``Template`` is that sub-graph as a value: a
build function plus the names of its inputs. adag calls the build function twice:

- Once with placeholders, to hash the graph. The template hash does not depend on any item.
- Once per item with the real values, to get the sub-graph that runs for that item.

Example::

    measure = Template(build=lambda key, item: Measure(text=item)())
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from adag.hashing import Placeholder, encode_value
from adag.step import StepResult


@dataclass(frozen=True, kw_only=True)
class Template:
    """A sub-graph built by ``build(*inputs)``, with one name per input.

    ``build`` must return a ``StepResult``. It may pass its inputs to steps, as a field or as a
    call input. It must not read their values: it is called once with placeholders, and a build
    that branches on a value would hash to something that is wrong for the real values.

    Fields:
        inputs: the name of each argument of ``build``. ``FanOutStep`` needs ``("key", "item")``.
        build: the function that returns the sub-graph.
    """

    build: Callable[..., StepResult[Any]]
    inputs: tuple[str, ...] = ("key", "item")

    def trace(self) -> StepResult[Any]:
        """Return the sub-graph built over placeholders.

        Raises:
            TemplateInputError: ``build`` used the value of an input.
            TypeError: ``build`` did not return a ``StepResult``.
        """
        return self._built(tuple(Placeholder(name) for name in self.inputs))

    def steps(self) -> tuple[StepResult[Any], ...] | None:
        """Return every step of the traced sub-graph, each after the steps it uses.

        Returns ``None`` when ``build`` cannot run over placeholders (it read an input, or it did
        not return a ``StepResult``). A step from outside the template that ``build`` closes over
        is in the tuple too.
        """
        try:
            root = self.trace()
        except TypeError:  # includes TemplateInputError
            return None
        ordered: dict[int, StepResult[Any]] = {}
        visiting: set[int] = set()

        def visit(result: StepResult[Any]) -> None:
            if id(result.step) in ordered or id(result.step) in visiting:
                return
            visiting.add(id(result.step))
            for source in result.sources.values():
                if isinstance(source, StepResult):
                    visit(source)  # pyright: ignore[reportUnknownArgumentType]
            ordered[id(result.step)] = result

        visit(root)
        return tuple(ordered.values())

    def bind(self, *values: Any) -> StepResult[Any]:
        """Return the sub-graph built over real values, one per input.

        Raises:
            TypeError: the number of values is not the number of inputs, or ``build`` did not
                return a ``StepResult``.
        """
        if len(values) != len(self.inputs):
            raise TypeError(
                f"template takes {len(self.inputs)} input(s) {self.inputs}, got {len(values)}."
            )
        return self._built(values)

    def hash(self) -> str | None:
        """Return the template hash, or ``None`` when a file or upstream in the graph blocks it.

        It is the key of the sub-graph built over placeholders, the same key machinery as a
        step. It changes when the code, a field or an upstream of the sub-graph changes.
        """
        graph_key = self.trace().content_hash()
        if graph_key is None:
            return None
        digest = hashlib.sha256()
        digest.update(b"TEMPLATE:" + encode_value(self.inputs) + graph_key.encode())
        return digest.hexdigest()[:12]

    def _built(self, values: tuple[Any, ...]) -> StepResult[Any]:
        graph = self.build(*values)
        if not isinstance(graph, StepResult):  # pyright: ignore[reportUnnecessaryIsInstance]  (build is user code)
            raise TypeError(f"Template build must return a StepResult; got {type(graph).__name__}.")
        return graph
