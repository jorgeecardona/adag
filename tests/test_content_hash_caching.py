"""Caching behaviour of content-hash computation — the two memoizations that fixed slow DAG startup.

class_source_hash() is memoized per class (inspect.getsource re-reads and re-tokenizes the
source file on every call, and it runs once per step instance across the whole graph).
StepResult.content_hash() is memoized per params-object identity (a deep chain otherwise
recomputes the same upstream hashes quadratically). Both sit on a hot path, so these tests
pin the contract: the cached value equals the freshly computed one, and the "not computed
yet" state stays distinct from a legitimately None hash.
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from adag import hashing
from adag.hashing import class_source_hash
from adag.io import FileSource
from adag.step import Step, StepResult


class Number(BaseModel):
    """Minimal result model."""

    value: int


@dataclass(frozen=True)
class Constant(Step[Number]):
    """A leaf step whose hash depends only on its own field."""

    n: int

    def run(self, n: int) -> Number:
        return Number(value=n)


@dataclass(frozen=True)
class FileStep(Step[Number]):
    """A step whose hash is blocked when its FileSource is missing."""

    data: FileSource

    def run(self, data: str) -> Number:
        return Number(value=len(data))


def test_class_source_hash_serves_from_cache(monkeypatch: Any):
    """The second call returns the cached hash without re-invoking inspect.getsource."""

    class Sample:
        def run(self) -> int:
            return 1

    hashing._class_source_hash_cache.clear()
    getsource_calls: list[type] = []
    real_getsource = hashing.inspect.getsource

    def counting_getsource(obj: Any) -> str:
        getsource_calls.append(obj)
        return real_getsource(obj)

    monkeypatch.setattr(hashing.inspect, "getsource", counting_getsource)

    first = class_source_hash(Sample)
    second = class_source_hash(Sample)

    assert first == second
    assert first is not None
    assert len(getsource_calls) == 1


def test_class_source_hash_caches_none_for_sourceless_class():
    """A dynamically built class has no source; None must be cached, not recomputed as a miss."""
    dynamic = type("Dynamic", (), {})
    hashing._class_source_hash_cache.clear()

    assert class_source_hash(dynamic) is None
    assert dynamic in hashing._class_source_hash_cache
    assert hashing._class_source_hash_cache[dynamic] is None
    assert class_source_hash(dynamic) is None


def test_content_hash_with_default_params_is_computed():
    """params=None is the common call; it must compute a real hash, not short-circuit to None.

    Regression guard for the memo's "not computed yet" flag: initialising the params slot to
    None would make the first content_hash() return the uninitialised None for every step.
    """
    graph = Constant(n=5)(n=5)
    assert graph.content_hash() is not None
    assert graph.content_hash() == graph.content_hash()


def test_content_hash_memoized_for_same_params_object(monkeypatch: Any):
    """Two calls with the same params object compute once; the second is served from the memo."""
    compute_calls: list[StepResult[Any]] = []
    real_compute = StepResult[Any]._compute_content_hash

    def counting_compute(self: StepResult[Any], params: dict[str, Any] | None = None) -> str | None:
        compute_calls.append(self)
        return real_compute(self, params)

    monkeypatch.setattr(StepResult, "_compute_content_hash", counting_compute)

    graph = Constant(n=5)(n=5)
    params: dict[str, Any] = {}
    first = graph.content_hash(params=params)
    second = graph.content_hash(params=params)

    assert first == second
    assert len(compute_calls) == 1


def test_content_hash_recomputes_for_new_params_object(monkeypatch: Any):
    """The memo is single-slot, keyed by identity — a different params object recomputes."""
    compute_calls: list[StepResult[Any]] = []
    real_compute = StepResult[Any]._compute_content_hash

    def counting_compute(self: StepResult[Any], params: dict[str, Any] | None = None) -> str | None:
        compute_calls.append(self)
        return real_compute(self, params)

    monkeypatch.setattr(StepResult, "_compute_content_hash", counting_compute)

    graph = Constant(n=5)(n=5)
    first = graph.content_hash(params={})
    second = graph.content_hash(params={})

    assert first == second
    assert len(compute_calls) == 2


def test_content_hash_none_for_missing_file_is_stable():
    """A blocked step hashes to None; the memo returns None on repeat calls without error.

    This is exactly the case the computed-yet flag protects: None is a real result, not a
    sign that computation hasn't happened.
    """
    missing = FileSource("/nonexistent/file.txt")
    graph = FileStep(data=missing)(data=missing)

    assert graph.content_hash() is None
    assert graph.content_hash() is None
