"""The async resolve path: AsyncStep + Pipeline.aexecute / StepResult.aresolve.

The sync engine cannot ``await`` a step body, so a step that does I/O (a DB AsyncSession, an HTTP/LLM
call) would have to be offloaded to a thread — defeating async concurrency and breaking single-shared-
resource contracts. The async path mirrors the sync one exactly (same memoization, same cache format,
same executor seam) but ``await``s upstream resolution and the step body, so async steps run ON the event
loop. Sync steps are unchanged: their inherited ``arun`` just runs ``run``, so a graph can MIX sync and
async steps and resolve the whole thing with ``aexecute``.

No pytest-asyncio: each test drives the coroutine with ``asyncio.run`` so there's no new test dependency.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from adag.deferred import ResolveContext
from adag.pipeline import Pipeline
from adag.step import AsyncStep, InputParam, Step, StepTimeout


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class Start(Step[Number]):
    """A SYNC leaf step producing a number."""

    n: int

    def run(self, n: int) -> Number:
        return Number(value=n)


@dataclass(frozen=True)
class AsyncDouble(AsyncStep[Number]):
    """An ASYNC step that awaits, then doubles its upstream number."""

    async def arun(self, source: Number) -> Number:
        await asyncio.sleep(0)  # a real suspension point
        return Number(value=source.value * 2)


def test_aexecute_resolves_an_async_step():
    """A graph whose terminal is an AsyncStep resolves through Pipeline.aexecute."""
    result = asyncio.run(Pipeline(AsyncDouble()(source=Start(n=3)(n=3))).aexecute())
    assert result.value == 6


def test_sync_step_runs_unchanged_on_the_async_path():
    """A SYNC step resolves fine under aexecute (its inherited arun delegates to run) — so sync and
    async steps mix freely in one graph.
    """
    # AsyncDouble consumes a sync Start; both resolve under one aexecute.
    result = asyncio.run(Pipeline(AsyncDouble()(source=Start(n=21)(n=21))).aexecute())
    assert result.value == 42


def test_pure_sync_pipeline_is_unchanged():
    """The synchronous execute() path is untouched."""
    assert Pipeline(Start(n=5)(n=5)).execute().value == 5


def test_async_step_on_sync_path_raises():
    """Running an AsyncStep through the synchronous path is a programming error — it raises clearly
    rather than returning an un-awaited coroutine.
    """
    pipeline = Pipeline(AsyncDouble()(source=Start(n=3)(n=3)))
    with pytest.raises(RuntimeError, match="AsyncStep"):
        pipeline.execute()


def test_async_executor_seam_intercepts_each_step():
    """A custom AsyncExecutor (carried on the context) intercepts every step, upstream first — the async
    mirror of the Executor seam, the hook a remote async executor would use.
    """
    ran: list[str] = []

    class RecordingAsyncExecutor:
        async def run(self, step: Any, context: ResolveContext, inputs: dict[str, Any]) -> Any:
            ran.append(type(step).__name__)
            return await step.aexecute(context, **inputs)

    result = asyncio.run(
        Pipeline(AsyncDouble()(source=Start(n=3)(n=3))).aexecute(
            async_executor=RecordingAsyncExecutor()
        )
    )
    assert result.value == 6
    assert ran == ["Start", "AsyncDouble"]  # upstream resolved before its consumer


def test_shared_upstream_resolved_once():
    """A StepResult consumed by two inputs of the same node resolves ONCE (context._resolved memo),
    on the async path too — so a non-idempotent/expensive upstream isn't re-run per consumer.
    """
    calls: list[int] = []

    @dataclass(frozen=True)
    class Source(AsyncStep[Number]):
        async def arun(self) -> Number:
            calls.append(1)
            return Number(value=7)

    @dataclass(frozen=True)
    class Add(AsyncStep[Number]):
        async def arun(self, a: Number, b: Number) -> Number:
            return Number(value=a.value + b.value)

    shared = Source()()
    assert asyncio.run(Pipeline(Add()(a=shared, b=shared)).aexecute()).value == 14
    assert calls == [1]


def test_cancellation_unwinds_without_starting_downstream():
    """Cancelling the awaiting task propagates CancelledError out of aexecute and no downstream step
    starts — the property the WebSocket supersede/coalesce relies on.
    """
    started: list[str] = []

    @dataclass(frozen=True)
    class Slow(AsyncStep[Number]):
        async def arun(self) -> Number:
            started.append("slow")
            await asyncio.sleep(10)  # blocks until cancelled
            return Number(value=1)

    @dataclass(frozen=True)
    class After(AsyncStep[Number]):
        async def arun(self, source: Number) -> Number:
            started.append("after")
            return Number(value=source.value + 1)

    pipeline = Pipeline(After()(source=Slow()()))

    async def go() -> None:
        task = asyncio.create_task(pipeline.aexecute())
        await asyncio.sleep(0.05)  # let Slow start and block on its sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert started == ["slow"]  # After never ran — cancellation unwound the in-flight resolution


def test_per_request_resource_flows_as_an_input_param():
    """A per-request resource (e.g. a DB AsyncSession) flows in like any other input — no special
    context field needed. This is exactly how the Labs engine would thread its AsyncSession.
    """
    seen: dict[str, Any] = {}

    @dataclass(frozen=True)
    class UsesSession(AsyncStep[Number]):
        async def arun(self, session: Any, n: int) -> Number:
            seen["session"] = session
            return Number(value=n)

    sentinel = object()
    graph = UsesSession()(session=InputParam("session"), n=InputParam("n"))
    result = asyncio.run(Pipeline(graph).aexecute(session=sentinel, n=5))
    assert result.value == 5
    assert seen["session"] is sentinel


def test_missing_param_raises_on_async_path():
    """Aexecute validates required params before resolving, same as execute."""
    graph = AsyncDouble()(source=Start(n=3)(n=3))
    # add a required param the call won't supply
    graph2 = AsyncDouble()(source=Start(n=3)(n=InputParam("n")))
    with pytest.raises(ValueError, match="Missing parameters"):
        asyncio.run(Pipeline(graph2).aexecute())
    # the well-formed one still resolves
    assert asyncio.run(Pipeline(graph).aexecute()).value == 6


@dataclass(frozen=True)
class SlowTimed(AsyncStep[Number]):
    """An async step whose body sleeps well past its timeout_s budget."""

    timeout_s: ClassVar[float] = 0.05

    async def arun(self) -> Number:
        await asyncio.sleep(10)  # would block forever without the timeout
        return Number(value=1)


def test_step_timeout_raises_step_timeout():
    """A step that exceeds its timeout_s budget fails as a clean StepTimeout instead of hanging the host."""
    with pytest.raises(StepTimeout, match="timeout_s"):
        asyncio.run(Pipeline(SlowTimed()()).aexecute())


def test_step_timeout_is_catchable_as_timeout_error():
    """StepTimeout subclasses the builtin TimeoutError, so callers can catch either."""
    assert issubclass(StepTimeout, TimeoutError)
    with pytest.raises(TimeoutError):
        asyncio.run(Pipeline(SlowTimed()()).aexecute())


def test_step_within_timeout_resolves_normally():
    """A step that finishes within timeout_s resolves as usual — the budget only fires on overrun."""

    @dataclass(frozen=True)
    class FastTimed(AsyncStep[Number]):
        timeout_s: ClassVar[float] = 5.0

        async def arun(self) -> Number:
            await asyncio.sleep(0)
            return Number(value=9)

    assert asyncio.run(Pipeline(FastTimed()()).aexecute()).value == 9


def test_no_timeout_is_unbounded_default():
    """timeout_s defaults to None (today's behaviour) — unbounded, no wait_for wrapping."""
    assert Step.timeout_s is None and AsyncDouble.timeout_s is None
    assert asyncio.run(Pipeline(AsyncDouble()(source=Start(n=4)(n=4))).aexecute()).value == 8


def test_pure_defaults_true_and_is_overridable_without_affecting_resolution():
    """`pure` defaults True (every existing step) and a generative step opts out — PROVENANCE only:
    impurity does not block resolution (or caching). No behavioural change.
    """
    assert Step.pure is True and AsyncDouble.pure is True

    @dataclass(frozen=True)
    class Generative(AsyncStep[Number]):
        pure: ClassVar[bool] = False

        async def arun(self) -> Number:
            return Number(value=0)

    assert Generative.pure is False
    assert asyncio.run(Pipeline(Generative()()).aexecute()).value == 0  # impure still resolves fine


def test_caller_schedules_levels_with_node_aresolve():
    """A caller sorts the graph into levels itself and gathers ``aresolve`` per level.

    ``StepResult.aresolve(context)`` is public: one shared ``ResolveContext`` memoizes every node,
    so a later level reuses the earlier results and nothing runs twice.
    """
    calls: list[str] = []

    @dataclass(frozen=True)
    class Leaf(AsyncStep[Number]):
        n: int

        async def arun(self, n: int) -> Number:
            calls.append(f"leaf{n}")
            return Number(value=n)

    @dataclass(frozen=True)
    class Sum(AsyncStep[Number]):
        async def arun(self, a: Number, b: Number) -> Number:
            calls.append("sum")
            return Number(value=a.value + b.value)

    a, b = Leaf(n=1)(), Leaf(n=2)()
    total = Sum()(a=a, b=b)
    context = ResolveContext(cache_dir=None, params={})

    async def run_levels() -> int:
        await asyncio.gather(a.aresolve(context), b.aresolve(context))
        (result,) = await asyncio.gather(total.aresolve(context))
        return result.value

    assert asyncio.run(run_levels()) == 3
    assert sorted(calls) == ["leaf1", "leaf2", "sum"]
