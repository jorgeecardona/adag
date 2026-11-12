"""What goes into a cache key, one component at a time.

``StepResult.explain_key`` and ``Pipeline.explain_keys`` return these types. The key itself is
built from the same components, so an explanation and its key cannot disagree.
"""

from __future__ import annotations

from enum import StrEnum


class ComponentKind(StrEnum):
    """The kind of one input to a cache key."""

    IDENTITY = "identity"
    """The step's class name or ``kind``; for a node, the step's own key."""
    CODE = "code"
    """A digest of the step class's source code."""
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
    SKIPPED = "skipped"
    """Not part of the key: a ``Config`` field, ``node_label``, or a value that cannot be
    hashed when ``strict_content_hash = False``."""
