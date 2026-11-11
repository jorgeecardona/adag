"""Tests for Pipeline graph validation: cycle detection, topological_order(), validate()."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from adag import CyclicPipelineError, Pipeline, Step, UnboundInputError


class Result(BaseModel):
    count: int


@dataclass(frozen=True)
class Source(Step[Result]):
    """Leaf step with no required inputs."""

    def run(self) -> Result:
        return Result(count=1)


@dataclass(frozen=True)
class Plus(Step[Result]):
    """Requires one wired input, ``prev``. ``bump`` distinguishes otherwise-equal nodes."""

    bump: int = 1

    def run(self, prev: Result) -> Result:
        return Result(count=prev.count + self.bump)


def test_topological_levels_groups_independent_steps():
    source = Source()()
    left = Plus(bump=1)(prev=source)
    right = Plus(bump=2)(prev=source)  # distinct config -> distinct node, independent of left
    pipeline = Pipeline.from_targets([left, right])

    levels = pipeline.topological_levels()
    assert levels[0] == [source.step]
    assert set(levels[1]) == {left.step, right.step}
    assert len(levels) == 2


def test_topological_order_lists_dependencies_before_dependents():
    source = Source()()
    chained = Plus()(prev=source)
    pipeline = Pipeline(chained)

    order = pipeline.topological_order()
    assert order.index(source.step) < order.index(chained.step)
    assert pipeline.topological_order() == pipeline.steps


def test_validate_passes_when_every_required_input_is_wired():
    source = Source()()
    chained = Plus()(prev=source)

    Pipeline(chained).validate()  # no raise — prev is wired, Source needs nothing


def test_validate_raises_on_unwired_required_input():
    unwired = Plus()()  # 'prev' is required but neither wired nor a dataclass field
    pipeline = Pipeline(unwired)

    with pytest.raises(UnboundInputError, match="prev"):
        pipeline.validate()


def test_cycle_detection_raises_at_construction():
    a = Plus()()
    b = Plus()(prev=a)
    a.sources["prev"] = b  # inject a -> b -> a, as a dynamically-built or deserialised graph could

    with pytest.raises(CyclicPipelineError):
        Pipeline(a)
