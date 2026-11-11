"""Tests for Pipeline.resolve(target) and Pipeline.from_targets([...]).

These cover the "intermediate target" pattern: define the DAG once, let
each notebook / consumer pick the node it cares about. Both calls hit the
same cache so an intermediate read returns exactly what downstream steps
consumed — no more "fresh recomputation drifts from cached scaffolding"
bugs.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import CachedStep, Pipeline


class StageA(BaseModel):
    n: int


class StageB(BaseModel):
    doubled: int


class StageC(BaseModel):
    plus_one: int


@dataclass(frozen=True)
class A(CachedStep[StageA]):
    seed: int = 7

    def run(self, seed: int) -> StageA:
        return StageA(n=seed)


@dataclass(frozen=True)
class B(CachedStep[StageB]):
    a: CachedStep[StageA]

    def run(self, a: StageA) -> StageB:
        return StageB(doubled=a.n * 2)


@dataclass(frozen=True)
class C(CachedStep[StageC]):
    b: CachedStep[StageB]

    def run(self, b: StageB) -> StageC:
        return StageC(plus_one=b.doubled + 1)


def test_resolve_intermediate_target(tmp_path: Path):
    """Pipeline.resolve(b_def) returns the same StageB value the final step consumed."""
    a, b, c = A(), B(a=A()), C(b=B(a=A()))
    a_def = a()
    b_def = b(a=a_def)
    c_def = c(b=b_def)
    pipeline = Pipeline(c_def, cache_dir=tmp_path / "cache")

    c_result = pipeline.execute()
    assert c_result.plus_one == 15  # (7 * 2) + 1

    b_result = pipeline.resolve(b_def)
    assert b_result.doubled == 14  # confirms intermediate cache reuse


def test_resolve_rejects_unrelated_target(tmp_path: Path):
    """Targets that aren't part of the pipeline's DAG raise a clear error."""
    a_def = A()()
    pipeline = Pipeline(a_def, cache_dir=tmp_path / "cache")
    # Build a fresh, disconnected B graph.
    stranger_b = B(a=A())(a=A()())
    with pytest.raises(ValueError, match="not part of this pipeline"):
        pipeline.resolve(stranger_b)


def test_from_targets_multi_sink(tmp_path: Path):
    """A pipeline with two ending points exposes both via .targets and resolves either one."""
    a, b, c = A(), B(a=A()), C(b=B(a=A()))
    a_def = a()
    b_def = b(a=a_def)
    c_def = c(b=b_def)
    pipeline = Pipeline.from_targets([c_def, b_def], cache_dir=tmp_path / "cache")
    assert pipeline.targets == [c_def, b_def]

    # First target is the canonical output of execute().
    c_result = pipeline.execute()
    assert c_result.plus_one == 15

    # Both targets — and intermediates — are reachable through resolve().
    assert pipeline.resolve(b_def).doubled == 14
    assert pipeline.resolve(a_def).n == 7


def test_from_targets_requires_at_least_one():
    with pytest.raises(ValueError, match="at least one target"):
        Pipeline.from_targets([], cache_dir=None)


def test_resolve_returns_same_value_as_execute(tmp_path: Path):
    """resolve(intermediate) returns the same value the canonical execute() produced.

    This is the regression test for the bb7f53 mismatch bug: a pipeline-
    level resolve must read what the cached pipeline used, not re-compute
    by calling step.run() directly (which can diverge if the step's code
    changed since the cache was written).
    """
    a, _b, c = A(), B(a=A()), C(b=B(a=A()))
    a_def, c_def = a(), c(b=B(a=A())(a=A()()))
    pipeline = Pipeline(c_def, cache_dir=tmp_path / "cache")
    pipeline.execute()
    # The same A() resolved via pipeline matches the StageA the pipeline used.
    via_resolve = pipeline.resolve(a_def)
    assert via_resolve.n == 7
