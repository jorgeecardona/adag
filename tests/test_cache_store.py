"""CacheStore backends — the pluggable key→text primitive behind the DAG cache.

Every backend must satisfy the same tiny contract (get/set/exists/delete/iter_keys over
str values), so ItemCache can be backed by the filesystem, Redis, a DB, etc.
These tests pin that contract against the filesystem, in-memory, SQLite, and SQLAlchemy
backends (the SQL one over a file-based sqlite URL). Redis/valkey is exercised
structurally with a fake client — it needs a live server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adag.cache_store import (
    CacheStore,
    FileSystemCacheStore,
    MemoryCacheStore,
    RedisCacheStore,
    SqlCacheStore,
    SqliteCacheStore,
)

ALL_BACKENDS = ["filesystem", "memory", "sqlite", "sql"]


def _store(tmp_path: Path, backend: str) -> CacheStore:
    """One disk-or-memory backend per name. `sql` uses a file-based sqlite URL so the
    SQLAlchemy path is exercised without a live Postgres (valkey == RedisCacheStore,
    covered separately by the fake-client test).
    """
    if backend == "filesystem":
        return FileSystemCacheStore(tmp_path)
    if backend == "memory":
        return MemoryCacheStore()
    if backend == "sqlite":
        return SqliteCacheStore(tmp_path.joinpath("cache.db"))
    if backend == "sql":
        return SqlCacheStore(url=f"sqlite:///{tmp_path.joinpath('sql_cache.db')}")
    raise ValueError(f"unknown backend: {backend}")


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_roundtrip_and_absence(tmp_path: Path, backend: str) -> None:
    store = _store(tmp_path, backend)
    assert store.get("missing") is None  # absence → None, never raises
    assert store.exists("missing") is False
    store.set("a", "hello")
    assert store.get("a") == "hello"
    assert store.exists("a") is True
    store.set("a", "world")  # overwrite
    assert store.get("a") == "world"
    store.delete("a")
    assert store.get("a") is None
    store.delete("a")  # delete-absent is a no-op


@pytest.mark.parametrize("backend", ALL_BACKENDS)
def test_nested_keys_and_prefix_scan(tmp_path: Path, backend: str) -> None:
    """Namespaced keys (the cache layer uses 'namespace/hash') round-trip, and the
    in-flight WAL pattern (a sibling key with an '.inflight' suffix) needs no special
    method — it's just another key under the same prefix.
    """
    store = _store(tmp_path, backend)
    store.set("ns/k1", "1")
    store.set("ns/k2", "2")
    store.set("other/k3", "3")
    store.set("ns/k1.inflight", "")  # WAL marker — same primitive
    assert set(store.iter_keys("ns/")) == {"ns/k1", "ns/k2", "ns/k1.inflight"}
    assert set(store.iter_keys("ns/")) - {"ns/k1.inflight"} == {
        "ns/k1",
        "ns/k2",
    }  # results vs markers by suffix
    assert set(store.iter_keys()) == {"ns/k1", "ns/k2", "other/k3", "ns/k1.inflight"}


def test_filesystem_rejects_path_traversal(tmp_path: Path) -> None:
    store = FileSystemCacheStore(tmp_path)
    with pytest.raises(ValueError):
        store.set("../escape", "x")


def test_filesystem_persists_across_instances(tmp_path: Path) -> None:
    FileSystemCacheStore(tmp_path).set("ns/k", "durable")
    assert (
        FileSystemCacheStore(tmp_path).get("ns/k") == "durable"
    )  # survives a new instance (real disk)


def test_sqlite_persists_across_instances(tmp_path: Path) -> None:
    SqliteCacheStore(tmp_path.joinpath("cache.db")).set("ns/k", "durable")
    assert (
        SqliteCacheStore(tmp_path.joinpath("cache.db")).get("ns/k") == "durable"
    )  # survives a new instance (single .db file)


def test_sql_persists_across_instances(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path.joinpath('sql_cache.db')}"
    SqlCacheStore(url=url).set("ns/k", "durable")
    assert (
        SqlCacheStore(url=url).get("ns/k") == "durable"
    )  # survives a new engine against the same URL


def test_sql_requires_url_or_engine() -> None:
    with pytest.raises(ValueError):
        SqlCacheStore()


def test_all_backends_satisfy_the_protocol(tmp_path: Path) -> None:
    assert isinstance(FileSystemCacheStore(tmp_path), CacheStore)
    assert isinstance(MemoryCacheStore(), CacheStore)
    assert isinstance(SqliteCacheStore(tmp_path.joinpath("c.db")), CacheStore)
    assert isinstance(SqlCacheStore(url=f"sqlite:///{tmp_path.joinpath('s.db')}"), CacheStore)


def test_redis_store_namespaces_keys_with_a_fake_client() -> None:
    """RedisCacheStore is pure delegation to a redis client — verify key namespacing +
    decode without a live server, using a minimal fake that mimics the redis API.
    """

    class FakeRedis:
        def __init__(self) -> None:
            self.d: dict[str, bytes] = {}

        def get(self, name: str) -> bytes | None:
            return self.d.get(name)

        def set(self, name: str, value: str, ex: int | None = None) -> None:
            self.d[name] = value.encode()

        def exists(self, name: str) -> int:
            return 1 if name in self.d else 0

        def delete(self, name: str) -> None:
            self.d.pop(name, None)

        def scan_iter(self, match: str) -> list[str]:
            import fnmatch

            return [k for k in self.d if fnmatch.fnmatch(k, match)]

    fake = FakeRedis()
    store = RedisCacheStore(client=fake, key_prefix="dag:")
    store.set("ns/k", "v")
    assert "dag:ns/k" in fake.d  # namespaced on the wire
    assert store.get("ns/k") == "v"  # bytes decoded back to str
    assert store.exists("ns/k") is True
    assert set(store.iter_keys("ns/")) == {"ns/k"}  # prefix returned WITHOUT the dag: namespace
    store.delete("ns/k")
    assert store.get("ns/k") is None
