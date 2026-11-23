"""What goes into a cache key, one component at a time.

``StepResult.explain_key`` and ``Pipeline.explain_keys`` return these types. The key itself is
built from the same components, so an explanation and its key cannot disagree.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adag.template import Template


class ComponentKind(StrEnum):
    """The kind of one input to a cache key."""

    IDENTITY = "identity"
    """The step's class name or ``kind``; for a node, the step's own key."""
    CODE = "code"
    """A digest of the step class's source code, or its ``code_version`` text."""
    PINNED = "pinned"
    """``Step.cache_key`` is set. It replaces identity, code and fields."""
    FIELD = "field"
    """A step field, encoded by value."""
    FILE = "file"
    """A ``FileSource`` field, hashed by the file's content."""
    NESTED_STEP = "nested_step"
    """A field that holds another step, hashed by that step's key."""
    UPSTREAM = "upstream"
    """An input wired to another node, hashed by that node's key."""
    PARAM = "param"
    """An input wired to an ``InputParam``, hashed by the run's value or the default."""
    DEFERRED = "deferred"
    """An input wired to another source, such as a ``FileSource``."""
    RAW = "raw"
    """An input passed as a literal value."""
    EXPANSION = "expansion"
    """A node that builds its sub-graph at run time (a template fan-out or a loop). The text says
    what is not known yet. It adds nothing to the key."""
    SKIPPED = "skipped"
    """Not part of the key: a ``Config`` field, ``node_label``, or a value that cannot be
    hashed when ``strict_content_hash = False``."""


@dataclass(frozen=True)
class Expansion:
    """How a node builds its sub-graph at run time: a template fan-out (``mapped``) or a loop.

    ``dag()`` draws the node with the template's steps inside it, and ``explain_key`` lists the
    ``note`` as an ``EXPANSION`` component.
    """

    shape: str
    template: Template
    note: str
    binds: tuple[tuple[str, str], ...] = ()
    """``(outer input, template input)`` pairs: which wired input feeds which template input."""
    feedback: str = ""
    """For a loop: the template input that receives the body's result for the next round."""
    caption: str = ""
    """A short sentence the frame shows under its body."""
    count: int | None = None
    """The number of branches when it is known before the run (a fan-out over a static dict)."""


_VALUE_WIDTH = 80


def _value_text(value: Any) -> str:
    """A short text form of a key component's value, for a person to read."""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= _VALUE_WIDTH else text[: _VALUE_WIDTH - 3] + "..."


@dataclass(frozen=True)
class KeyComponent:
    """One input to a cache key.

    ``digest`` is the first 12 hex characters of sha256 over the bytes this component adds to the
    key. Two components with the same ``(kind, name)`` and the same digest added the same bytes.
    It is empty for a ``SKIPPED`` component and for one that blocked the key.
    """

    kind: ComponentKind
    name: str
    value: str
    digest: str

    @staticmethod
    def build(kind: ComponentKind, name: str, value: Any, chunk: bytes | None) -> KeyComponent:
        """Build a component from one ``_key_chunks`` tuple."""
        digest = hashlib.sha256(chunk).hexdigest()[:12] if chunk else ""
        return KeyComponent(kind=kind, name=name, value=_value_text(value), digest=digest)


@dataclass(frozen=True)
class KeyExplanation:
    """Every component of one node's cache key, in the order adag hashes them.

    ``key`` is ``None`` when a component blocked it (a missing file, an upstream with no key);
    ``blocked_by`` then names that component. An ``UPSTREAM`` component shows the upstream step
    and its key: explain that node next to follow a change up the graph.
    """

    step: str
    label: str | None
    key: str | None
    blocked_by: str | None
    key_format: int
    components: tuple[KeyComponent, ...]

    def redacted(self) -> KeyExplanation:
        """A copy with the value of every ``SKIPPED`` component removed.

        A skipped component is not in the key, and its value can be a client, a path or a
        secret (a ``Config`` field). The key record is written to disk or to a shared store, so
        it keeps only the name.
        """
        return replace(
            self,
            components=tuple(
                replace(c, value="") if c.kind is ComponentKind.SKIPPED else c
                for c in self.components
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """The JSON form, as written to the key record."""
        data = asdict(self)
        data["components"] = [{**asdict(c), "kind": c.kind.value} for c in self.components]
        return data

    @staticmethod
    def from_dict(data: dict[str, Any]) -> KeyExplanation:
        """Rebuild an explanation from ``to_dict`` output."""
        return KeyExplanation(
            step=data["step"],
            label=data["label"],
            key=data["key"],
            blocked_by=data["blocked_by"],
            key_format=data["key_format"],
            components=tuple(
                KeyComponent(
                    kind=ComponentKind(c["kind"]),
                    name=c["name"],
                    value=c["value"],
                    digest=c["digest"],
                )
                for c in data["components"]
            ),
        )

    def __str__(self) -> str:
        title = f"{self.step}" + (f" ({self.label})" if self.label else "")
        status = self.key if self.key is not None else f"blocked by {self.blocked_by}"
        rows = [(c.kind.value, c.name or "-", c.value, c.digest or "-") for c in self.components]
        widths = [max([len(r[i]) for r in rows] + [1]) for i in range(3)]
        lines = [f"{title}  key={status}  format={self.key_format}"]
        for kind, name, value, digest in rows:
            lines.append(
                f"  {kind:<{widths[0]}}  {name:<{widths[1]}}  {value:<{widths[2]}}  {digest}"
            )
        return "\n".join(lines)
