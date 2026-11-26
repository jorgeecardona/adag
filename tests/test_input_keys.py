"""An input that adag cannot hash blocks the cache key, so no step reuses another's result.

A ``Deferred`` subclass of your own used to be keyed by its class name only: two inputs with
different values shared one key, and the second run loaded the first run's result.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import (
    Deferred,
    DirSource,
    FileSource,
    NullObserver,
    Pipeline,
    ResolveContext,
    Step,
    aresolve_all,
)
from adag.cache_store import MemoryCacheStore
from adag.visualization import node_metadata, walk


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class Unhashed(Deferred[int]):
    """An input with no ``content_hash``."""

    value: int

    def resolve(self, context: ResolveContext) -> int:
        return self.value


@dataclass(frozen=True)
class Hashed(Unhashed):
    """The same input with a ``content_hash``."""

    def content_hash(self) -> str | None:
        return f"hashed-{self.value}"


@dataclass(frozen=True)
class Unready(Unhashed):
    """An input that says it has no hash now."""

    def content_hash(self) -> str | None:
        return None


@dataclass(frozen=True)
class Double(Step[Number]):
    def run(self, x: int) -> Number:
        return Number(value=2 * x)


@dataclass(frozen=True)
class Same(Step[Number]):
    def run(self, upstream: Number) -> Number:
        return upstream


@dataclass(frozen=True)
class Length(Step[Number]):
    def run(self, text: str) -> Number:
        return Number(value=len(text))


@pytest.mark.parametrize("source", [Unhashed, Unready])
def test_an_input_with_no_hash_blocks_the_key_and_every_key_downstream(
    source: type[Unhashed],
) -> None:
    node = Double()(x=source(1))
    downstream = Same()(upstream=node)

    assert node.content_hash() is None
    assert downstream.content_hash() is None
    assert node.explain_key().blocked_by == "x"


@pytest.mark.parametrize("source", [Unhashed, Unready])
def test_a_second_run_with_another_value_computes_it(source: type[Unhashed]) -> None:
    store = MemoryCacheStore()

    first = Pipeline(Double()(x=source(1)), store=store).execute(observer=NullObserver())
    second = Pipeline(Double()(x=source(2)), store=store).execute(observer=NullObserver())

    assert (first.value, second.value) == (2, 4)


def test_two_inputs_with_no_hash_do_not_share_a_result_in_aresolve_all() -> None:
    targets = [Double()(x=Unhashed(1)), Double()(x=Unhashed(2))]

    results = asyncio.run(aresolve_all(targets, ResolveContext()))

    assert [result.value for result in results] == [2, 4]


def test_an_input_with_a_hash_keys_the_step_by_it() -> None:
    store = MemoryCacheStore()
    node = Double()(x=Hashed(1))

    assert node.content_hash() == Double()(x=Hashed(1)).content_hash()
    assert node.content_hash() != Double()(x=Hashed(2)).content_hash()
    Pipeline(node, store=store).execute(observer=NullObserver())
    assert Double()(x=Hashed(1)).is_cached(store)


@pytest.mark.parametrize("source", [FileSource, DirSource])
def test_a_missing_path_wired_as_an_input_blocks_the_key(
    source: type[FileSource] | type[DirSource], tmp_path: Path
) -> None:
    node = Length()(text=source(tmp_path / "missing"))

    assert node.content_hash() is None


def test_dag_draws_a_node_with_a_blocked_input_and_its_downstream_as_blocked(
    tmp_path: Path,
) -> None:
    downstream = Same()(upstream=Length()(text=FileSource(tmp_path / "missing")))
    nodes, _ = walk(downstream)

    states = {data.title: data.state for data in node_metadata(nodes, cache_dir=tmp_path)}

    assert states["Length"] == "BLOCKED"
    assert states["Same"] == "BLOCKED"
