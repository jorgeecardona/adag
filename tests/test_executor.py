"""The executor seam: ResolveContext carries a pluggable Executor that runs each step.

LocalExecutor (the default) runs steps in-process — identical to the pre-seam behaviour.
A custom executor can intercept every step execution; that interception point is the hook a
future remote executor uses to dispatch a node to another process or machine instead of
running it inline. The graph, the cache, and the wiring are unchanged — only *where* a step
runs differs, and that is a property of the run (the context), not of the graph.
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from adag.deferred import ResolveContext
from adag.fan_out import FanOutStep
from adag.pipeline import Pipeline
from adag.step import Step


class Number(BaseModel):
    """Minimal result model."""

    value: int


@dataclass(frozen=True)
class Start(Step[Number]):
    """Leaf step producing a number."""

    n: int

    def run(self, n: int) -> Number:
        return Number(value=n)


@dataclass(frozen=True)
class Double(Step[Number]):
    """Doubles its upstream number."""

    def run(self, source: Number) -> Number:
        return Number(value=source.value * 2)


def build_two_step_graph():
    """Start(3) -> Double, expected terminal value 6."""
    start = Start(n=3)(n=3)
    return Double()(source=start)


def test_custom_executor_runs_each_step():
    """A custom executor passed to execute() intercepts every step, upstream first."""
    pipeline = Pipeline(build_two_step_graph())

    class RecordingExecutor:
        def __init__(self) -> None:
            self.ran: list[str] = []

        def run(self, step: Any, context: ResolveContext, inputs: dict[str, Any]) -> Any:
            self.ran.append(type(step).__name__)
            return step.execute(context, **inputs)

    recorder = RecordingExecutor()
    result = pipeline.execute(executor=recorder)

    assert result.value == 6
    assert recorder.ran == ["Start", "Double"]


def test_default_executor_runs_in_process():
    """With no executor, the default LocalExecutor runs steps in-process — unchanged behaviour."""
    assert Pipeline(build_two_step_graph()).execute().value == 6


def test_fan_out_branches_route_through_executor():
    """Each FanOutStep branch resolves through the same executor as the fan-out node itself.

    This is the property a future Batch executor relies on to dispatch branches: the fan-out
    node and every per-key sub-DAG all pass through context.executor.run, no special casing.
    """
    fan = FanOutStep(over={"a": 1, "b": 2}, factory=lambda key, value: Start(n=value)(n=value))()
    pipeline = Pipeline(fan)

    class RecordingExecutor:
        def __init__(self) -> None:
            self.ran: list[str] = []

        def run(self, step: Any, context: ResolveContext, inputs: dict[str, Any]) -> Any:
            self.ran.append(type(step).__name__)
            return step.execute(context, **inputs)

    recorder = RecordingExecutor()
    result = pipeline.execute(executor=recorder)

    assert result == {"a": Number(value=1), "b": Number(value=2)}
    assert recorder.ran.count("FanOutStep") == 1
    assert recorder.ran.count("Start") == 2
