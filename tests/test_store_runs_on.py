"""``runs_on`` on a cache store: where its calls run on the async path."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine, create_mock_engine

from adag._store_io import in_thread
from adag.cache_store import (
    CacheStore,
    FileSystemCacheStore,
    MemoryCacheStore,
    RedisCacheStore,
    SqlCacheStore,
    SqliteCacheStore,
)


class Plain:
    """A third-party store written before ``runs_on``."""

    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str) -> None: ...

    def exists(self, key: str) -> bool:
        return False

    def delete(self, key: str) -> None: ...

    def iter_keys(self, prefix: str = "") -> Iterable[str]:
        return []


def _mock_engine(url: str) -> Engine:
    def run(sql: Any, *args: Any, **kwargs: Any) -> None: ...

    return cast("Engine", create_mock_engine(url, run))


def test_local_stores_run_on_the_loop(tmp_path: Path) -> None:
    stores: list[CacheStore] = [
        FileSystemCacheStore(tmp_path),
        MemoryCacheStore(),
        SqliteCacheStore(tmp_path / "cache.db"),
        SqlCacheStore(url=f"sqlite:///{tmp_path / 'sql.db'}"),
        SqlCacheStore(url="sqlite://"),
    ]

    assert [in_thread(store) for store in stores] == [False] * len(stores)


def test_network_stores_run_in_a_thread() -> None:
    assert in_thread(RedisCacheStore(client=cast("Any", object())))
    assert in_thread(SqlCacheStore(engine=_mock_engine("postgresql://")))


def test_a_store_without_the_attribute_runs_on_the_loop_and_is_still_a_cache_store() -> None:
    assert not in_thread(Plain())
    assert isinstance(Plain(), CacheStore)


def test_an_instance_can_choose_the_loop() -> None:
    store = RedisCacheStore(client=cast("Any", object()))
    store.runs_on = "loop"

    assert not in_thread(store)


def test_a_subclass_choice_wins(tmp_path: Path) -> None:
    class Remote(FileSystemCacheStore):
        runs_on = "thread"

    class Near(SqlCacheStore):
        runs_on = "loop"

    assert in_thread(Remote(tmp_path))
    assert not in_thread(Near(engine=_mock_engine("postgresql://")))


def test_an_unknown_value_is_refused() -> None:
    store = MemoryCacheStore()
    store.runs_on = cast("Any", "process")

    with pytest.raises(ValueError, match="MemoryCacheStore.runs_on is 'process'"):
        in_thread(store)
