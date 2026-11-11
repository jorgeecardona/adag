"""Pipeline(store=...) keeps step results and BatchStep item caches in any CacheStore."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from adag import BatchItem, BatchStep, InputParam, Pipeline, Step
from adag.cache_store import (
    CacheStore,
    FileSystemCacheStore,
    MemoryCacheStore,
    SqlCacheStore,
    SqliteCacheStore,
    store_from_url,
)
from adag.cli import load_pipeline_module
from adag.dispatch import SubprocessDispatcher

RUNS: list[str] = []


class Number(BaseModel):
    value: int


class Greeting(BaseModel):
    text: str


class Greetings(BaseModel):
    items: list[Greeting]


@dataclass(frozen=True)
class Square(Step[Number]):
    def run(self, x: int) -> Number:
        RUNS.append("square")
        return Number(value=x * x)


@dataclass(frozen=True)
class Greet(BatchStep[Greetings, Greeting]):
    items_source_keys = ("names",)  # item keys come from each name, not the whole list

    def items_data(self, names: list[str]) -> list[str]:
        return names

    def process_item(self, item: BatchItem, names: list[str]) -> Greeting:
        RUNS.append(f"greet:{item.data}")
        return Greeting(text=f"hello {item.data}")

    def assemble(self, results: list[Greeting], names: list[str]) -> Greetings:
        return Greetings(items=results)


def _stores(tmp_path: Path) -> dict[str, CacheStore]:
    return {
        "memory": MemoryCacheStore(),
        "filesystem": FileSystemCacheStore(tmp_path / "fs"),
        "sqlite": SqliteCacheStore(tmp_path / "cache.db"),
        "sql": SqlCacheStore(url=f"sqlite:///{tmp_path / 'sql.db'}"),
    }


@pytest.mark.parametrize("name", ["memory", "filesystem", "sqlite", "sql"])
def test_step_results_live_in_the_store(tmp_path: Path, name: str):
    store = _stores(tmp_path)[name]
    pipeline = Pipeline(Square()(x=InputParam[int]("x", value_type=int)), store=store)
    RUNS.clear()
    assert pipeline.execute(x=4).value == 16
    assert pipeline.execute(x=4).value == 16
    assert RUNS == ["square"]  # the second run is a cache hit from the store
    assert any(key.endswith(".done") for key in store.iter_keys())


@pytest.mark.parametrize("name", ["memory", "filesystem", "sqlite", "sql"])
def test_batch_item_cache_lives_in_the_store(tmp_path: Path, name: str):
    store = _stores(tmp_path)[name]
    names = InputParam[list[str]]("names")
    pipeline = Pipeline(Greet()(names=names), store=store)
    RUNS.clear()
    pipeline.execute(names=["ana", "bo"])
    pipeline.execute(names=["ana", "bo", "cy"])
    assert RUNS == ["greet:ana", "greet:bo", "greet:cy"]  # items reused across runs
    assert any(key.startswith("greet/") for key in store.iter_keys())


def test_store_without_cache_dir_writes_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    Pipeline(Greet()(names=InputParam[list[str]]("names")), store=MemoryCacheStore()).execute(
        names=["ana"]
    )
    assert list(tmp_path.iterdir()) == []


def test_store_from_url_picks_the_backend(tmp_path: Path):
    assert isinstance(store_from_url(str(tmp_path / "dir")), FileSystemCacheStore)
    assert isinstance(store_from_url(f"file://{tmp_path / 'dir'}"), FileSystemCacheStore)
    sqlite_store = store_from_url(f"sqlite:///{tmp_path / 'c.db'}")
    assert isinstance(sqlite_store, SqliteCacheStore)
    assert sqlite_store.path == tmp_path / "c.db"
    assert isinstance(store_from_url(f"sqlite+pysqlite:///{tmp_path / 's.db'}"), SqlCacheStore)
    with pytest.raises(ValueError, match="cannot be shared"):
        store_from_url("memory:")


FAN_OUT_SOURCE = """
import os
from dataclasses import dataclass

from pydantic import BaseModel

from adag import FanOutStep, Step


class Doubled(BaseModel):
    value: int
    pid: int


@dataclass(frozen=True)
class Double(Step[Doubled]):
    n: int

    def run(self, n: int) -> Doubled:
        return Doubled(value=n * 2, pid=os.getpid())


per_key = FanOutStep(
    over={"a": 1, "b": 2},
    factory=lambda key, n: Double(n=n)(),
    pipeline_module=__file__,
    fan_out_var="per_key",
)()
"""


def test_fan_out_rejoins_through_a_sqlite_store_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Worker processes write to a SQLite file; no cache directory exists at all."""
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setenv("PYTHONPATH", src_path)
    path = tmp_path / "pipeline.py"
    path.write_text(FAN_OUT_SOURCE)
    module = load_pipeline_module(path)
    db = tmp_path / "rendezvous.db"
    url = f"sqlite:///{db}"

    pipeline: Pipeline[Any] = Pipeline(module.per_key, store=store_from_url(url))
    result = pipeline.execute(dispatcher=SubprocessDispatcher(store_url=url))

    assert {key: r.value for key, r in result.items()} == {"a": 2, "b": 4}
    assert all(r.pid != os.getpid() for r in result.values())
    left = {p.name for p in tmp_path.iterdir()} - {"__pycache__"}
    assert left == {"pipeline.py", "rendezvous.db"}  # no cache directory was created


def test_remote_dispatch_with_a_store_needs_a_store_url(tmp_path: Path):
    path = tmp_path / "pipeline.py"
    path.write_text(FAN_OUT_SOURCE)
    module = load_pipeline_module(path)
    pipeline: Pipeline[Any] = Pipeline(module.per_key, store=MemoryCacheStore())
    with pytest.raises(ValueError, match="store_url"):
        pipeline.execute(dispatcher=SubprocessDispatcher())
