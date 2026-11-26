"""The store calls one node makes, in order. Both paths make the same ones."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from adag import InputParam, Pipeline, Step
from adag.cache_store import MemoryCacheStore

n = InputParam[int]("n")


class Num(BaseModel):
    n: int


@dataclass(frozen=True)
class Double(Step[Num]):
    def run(self, n: int) -> Num:
        return Num(n=n * 2)


class Recording(MemoryCacheStore):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    def get(self, key: str) -> str | None:
        self.calls.append(("get", key))
        return super().get(key)

    def set(self, key: str, value: str) -> None:
        self.calls.append(("set", key))
        super().set(key, value)

    def exists(self, key: str) -> bool:
        self.calls.append(("exists", key))
        return super().exists(key)


def _execute(pipe: Pipeline[Num]) -> Num:
    return pipe.execute(n=3)


def _aexecute(pipe: Pipeline[Num]) -> Num:
    return asyncio.run(pipe.aexecute(n=3))


@pytest.mark.parametrize("run", [_execute, _aexecute], ids=["sync", "async"])
def test_store_calls_of_a_miss_then_a_hit(run: Callable[[Pipeline[Num]], Num]) -> None:
    store = Recording()
    node = Double()(n=n)
    pipe = Pipeline(output=node, store=store)
    key = node.content_hash(params={"n": 3})
    assert key is not None

    assert run(pipe) == Num(n=6)
    cold = list(store.calls)
    store.calls.clear()
    assert run(pipe) == Num(n=6)

    # ``aresolve_all`` asks once more per node, to skip the inputs of a saved result.
    walk = [("exists", f"{key}.done")] if run is _aexecute else []
    assert cold == [
        *walk,
        ("exists", f"{key}.done"),
        ("get", key),
        ("set", key),
        ("set", f"{key}.done"),
        ("set", f".keys/{key}.json"),
    ]
    assert store.calls == [*walk, ("exists", f"{key}.done"), ("get", key)]
