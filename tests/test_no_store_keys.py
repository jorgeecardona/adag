"""A run without a store computes keys only to find twins, so a key must never cost the run (#21).

A key that cannot be computed means "no key": the node runs on its own. Each node computes it
once, whatever the result.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, cast

import pytest
from pydantic import BaseModel

from adag import (
    Deferred,
    FanOutStep,
    InputParam,
    NullObserver,
    Pipeline,
    ResolveContext,
    Step,
    StepResult,
    Template,
)
from adag.cache_store import MemoryCacheStore
from adag.hashing import TemplateInputError


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class First(Step[Number]):
    def run(self, session: object) -> Number:
        return Number(value=0)


@dataclass(frozen=True)
class Next(Step[Number]):
    def run(self, upstream: Number) -> Number:
        return Number(value=upstream.value + 1)


def test_a_key_that_cannot_be_computed_is_tried_once_per_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node: StepResult[Number] = First()(session=InputParam[object]("session"))
    for _ in range(30):
        node = Next()(upstream=node)
    computed: list[StepResult[Any]] = []
    real: Any = vars(StepResult)["_compute_content_hash"]

    def counting(result: StepResult[Any], params: dict[str, Any] | None = None) -> str | None:
        computed.append(result)
        return real(result, params)

    monkeypatch.setattr(StepResult, "_compute_content_hash", counting)

    out = Pipeline(node).execute(observer=NullObserver(), session=object())

    assert out.value == 30
    assert len(computed) == 31


@dataclass(frozen=True)
class RaisingInput(Deferred[int]):
    def resolve(self, context: ResolveContext) -> int:
        return 1

    def content_hash(self) -> str | None:
        raise KeyError("not here")


@dataclass(frozen=True)
class Double(Step[Number]):
    def run(self, x: int) -> Number:
        return Number(value=2 * x)


@dataclass(frozen=True)
class OwnHashRaises(Step[Number]):
    def content_hash(self) -> str | None:
        raise RuntimeError("no hash today")

    def run(self) -> Number:
        return Number(value=7)


@dataclass(frozen=True)
class Plus(Step[Number]):
    n: int

    def run(self, n: int) -> Number:
        return Number(value=n)


def _adds_to_the_item(key: str, item: int) -> StepResult[Number]:
    return Plus(n=item + 1)()


def _reads_its_input() -> StepResult[Any]:
    template = Template(build=_adds_to_the_item)
    fan = FanOutStep[str, Number](over={"a": 1}, template=template)
    return cast("StepResult[Any]", fan())


@pytest.mark.parametrize("run_async", [False, True])
def test_a_content_hash_that_raises_does_not_fail_a_run_without_a_store(run_async: bool) -> None:
    for node in (Double()(x=RaisingInput()), OwnHashRaises()()):
        pipeline = Pipeline(node)
        if run_async:
            asyncio.run(pipeline.aexecute(observer=NullObserver()))
        else:
            pipeline.execute(observer=NullObserver())


@pytest.mark.parametrize("run_async", [False, True])
def test_a_template_that_reads_its_input_runs_without_a_store(run_async: bool) -> None:
    pipeline = Pipeline(_reads_its_input())

    if run_async:
        results = asyncio.run(pipeline.aexecute(observer=NullObserver()))
    else:
        results = pipeline.execute(observer=NullObserver())

    assert results["a"].value == 2


def test_with_a_store_the_error_of_a_content_hash_is_raised() -> None:
    pipeline = Pipeline(Double()(x=RaisingInput()), store=MemoryCacheStore())

    with pytest.raises(KeyError, match="not here") as raised:
        pipeline.execute(observer=NullObserver())
    # The traceback still ends in the line of the user that raised.
    assert raised.traceback[-1].name == "content_hash"
    with pytest.raises(TemplateInputError):
        Pipeline(_reads_its_input(), store=MemoryCacheStore()).execute(observer=NullObserver())
