"""execute(parallel=True) runs each level's workers inside a copied context, so ambient ContextVars
propagate into them. Two independent nodes form one level, exercising the thread-pool path (a single-node
level runs inline in the calling thread and needs no copy).
"""

from contextvars import ContextVar
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from adag import NullObserver, Pipeline, Step

# These tests cover max_workers and parallel, which are deprecated and removed in 1.0.
pytestmark = pytest.mark.filterwarnings("ignore::adag.AdagDeprecationWarning")

_LEVEL_MARKER: ContextVar[str] = ContextVar("level_marker", default="unset")


class Marker(BaseModel):
    value: str


@dataclass(frozen=True, eq=False)
class ReadLevelMarker(Step[Marker]):
    """Reads the ambient ContextVar inside its level-worker body."""

    def run(self) -> Marker:
        return Marker(value=_LEVEL_MARKER.get())


@dataclass(frozen=True, eq=False)
class JoinMarkers(Step[Marker]):
    def run(self, left: Marker, right: Marker) -> Marker:
        return Marker(value=f"{left.value},{right.value}")


def test_context_var_propagates_into_parallel_level_workers() -> None:
    left = ReadLevelMarker()()
    right = ReadLevelMarker()()
    pipeline = Pipeline(JoinMarkers()(left=left, right=right))

    token = _LEVEL_MARKER.set("bound")
    try:
        result = pipeline.execute(parallel=True, observer=NullObserver())
    finally:
        _LEVEL_MARKER.reset(token)

    assert result.value == "bound,bound"
