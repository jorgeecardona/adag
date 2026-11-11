"""CacheStore — the pluggable key→text backend behind the DAG cache.

The DAG's caches (``ItemCache`` per-item results, ``CachedStep`` step results, the
dispatch "cache-as-IPC" rejoin) all reduce to one tiny primitive: **put a piece of
text somewhere under a key, and load it back later**. Today that primitive is
hard-wired to the local filesystem, which is fine for offline/CLI/single-process
runs but wrong for a deployed multi-instance service — there the cache must live in
a SHARED store (Redis, a database, an object store, …).

`CacheStore` is that primitive, deliberately minimal so a user can back it with
anything in a few lines: ``get`` / ``set`` / ``exists`` / ``delete`` / ``iter_keys``,
all over plain ``str`` values. Higher-level concerns (the schema-version stamp, the
in-flight WAL, JSON (de)serialization) stay in ``ItemCache`` and are expressed PURELY
in terms of these five operations — e.g. an in-flight marker is just another key with
an ``.inflight`` suffix — so a custom backend never has to know about them.

Backends split into two tiers by dependency. The rule: **anything backed by the
stdlib is always available** — a plain ``adag`` install gives you these with
no extra install. Backends that need a third-party driver are opt-in behind an extra.

Always available (stdlib only):
- ``FileSystemCacheStore`` — the default; one text file per key, atomic writes, nested
  keys as sub-paths (the current on-disk behaviour; existing pipelines are unchanged).
- ``SqliteCacheStore`` — a durable single-file cache on stdlib ``sqlite3``; "persistence
  without standing up a server", with tighter concurrent-writer locking than loose files.
- ``MemoryCacheStore`` — in-process dict, for tests and single-process runs.

Optional (a SHARED backend for deployed multi-instance services, behind a conditional
import + an install extra — the driver is NEVER required just to import this module):
- ``SqlCacheStore`` — any SQLAlchemy-supported database (Postgres, MySQL, sqlite, …),
  so the cache can live in the database a service already runs. Extra: ``adag[sql]``.
- ``RedisCacheStore`` — redis / valkey (redis-py speaks the valkey protocol too).
  Extra: ``adag[redis]``.

A backend must be CONSISTENT across the processes that share it (the dispatch path writes
on a worker and reads back on the orchestrator): Redis, a SQL database, or a shared SQLite
file satisfy this; a per-pod local filesystem would not.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from adag.io import atomic_write_text

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy import Engine

logger = structlog.get_logger()


@runtime_checkable
class CacheStore(Protocol):
    """A string key → string value store. The whole contract a cache backend must
    satisfy; implement these five methods over Redis, a DB, S3, memory — anything.

    Keys are opaque strings the cache layer builds (content hashes, namespaced like
    ``"namespace/abc123"``). Values are UTF-8 text (the cache layer JSON-encodes its
    own payloads). ``get`` returns ``None`` for a missing key (never raises for absence).
    """

    def get(self, key: str) -> str | None:
        """Return the stored text for ``key``, or ``None`` if absent."""
        ...

    def set(self, key: str, value: str) -> None:
        """Store ``value`` under ``key`` (overwrites). Should be atomic per key."""
        ...

    def exists(self, key: str) -> bool:
        """True if ``key`` has a stored value."""
        ...

    def delete(self, key: str) -> None:
        """Remove ``key``. A no-op if it is absent (never raises for absence)."""
        ...

    def iter_keys(self, prefix: str = "") -> Iterable[str]:
        """Yield every stored key starting with ``prefix`` (all keys if empty)."""
        ...


class FileSystemCacheStore:
    """Default backend: one text file per key under ``root``. Keys may contain ``/``
    to nest into sub-directories. Writes are atomic (temp file + rename), matching the
    current ItemCache/CachedStep on-disk semantics so nothing changes for local runs.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        if ".." in Path(key).parts:
            raise ValueError(f"unsafe cache key (path traversal): {key!r}")
        return self.root.joinpath(key)

    def get(self, key: str) -> str | None:
        path = self._path(key)
        try:
            return path.read_text()
        except FileNotFoundError:
            return None

    def set(self, key: str, value: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, value)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def iter_keys(self, prefix: str = "") -> Iterable[str]:
        if not self.root.exists():
            return
        for p in self.root.rglob("*"):
            if p.is_file():
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    yield key


class _RedisLike(Protocol):
    """The slice of the redis client API ``RedisCacheStore`` calls — lets the store
    be fully typed without a hard dependency on redis' own stubs (redis is an
    optional, lazily-imported backend dependency).
    """

    def get(self, name: str) -> object: ...
    def set(self, name: str, value: str, ex: int | None = ...) -> object: ...
    def exists(self, name: str) -> int: ...
    def delete(self, name: str) -> object: ...
    def scan_iter(self, match: str) -> Iterable[object]: ...


class RedisCacheStore:
    """Shared backend for deployed services. Keys are namespaced under ``key_prefix``
    so one Redis can host many apps. ``redis`` is imported lazily — only constructing
    this class (or passing a client) needs the package installed.

    Pass an existing client (``RedisCacheStore(client=...)``) or a URL
    (``RedisCacheStore(url="redis://…")``). ``ttl_seconds`` optionally expires entries
    (None = persist; the DAG cache is content-addressed, so stale entries can't be
    wrong — TTL is purely an eviction/cost knob).
    """

    def __init__(
        self,
        *,
        client: _RedisLike | None = None,
        url: str | None = None,
        key_prefix: str = "dag:",
        ttl_seconds: int | None = None,
    ) -> None:
        resolved: _RedisLike
        if client is None:
            try:
                # Optional backend dep, imported lazily; pyright can't resolve it when
                # it isn't installed, which is a supported case (filesystem is default).
                import redis  # pyright: ignore[reportMissingImports]
            except ImportError as exc:  # pragma: no cover - exercised only without redis installed
                raise ImportError(
                    "RedisCacheStore needs the 'redis' package: pip install redis"
                ) from exc
            # reportAssignmentType: when redis IS installed, redis-py's own typing makes the concrete
            # Redis structurally incompatible with _RedisLike (its get/set bind through an invariant
            # _is_async_client), so the protocol decoupling needs this ignore too.
            resolved = redis.Redis.from_url(url or "redis://localhost:6379/0")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue, reportUnknownVariableType, reportAssignmentType]
        else:
            resolved = client
        self._client: _RedisLike = resolved
        self.key_prefix = key_prefix
        self.ttl_seconds = ttl_seconds

    def _k(self, key: str) -> str:
        return f"{self.key_prefix}{key}"

    @staticmethod
    def _text(raw: object) -> str:
        return raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)

    def get(self, key: str) -> str | None:
        raw = self._client.get(self._k(key))
        return None if raw is None else self._text(raw)

    def set(self, key: str, value: str) -> None:
        self._client.set(self._k(key), value, ex=self.ttl_seconds)

    def exists(self, key: str) -> bool:
        return bool(self._client.exists(self._k(key)))

    def delete(self, key: str) -> None:
        self._client.delete(self._k(key))

    def iter_keys(self, prefix: str = "") -> Iterable[str]:
        match = f"{self.key_prefix}{prefix}*"
        for raw in self._client.scan_iter(match=match):
            yield self._text(raw)[len(self.key_prefix) :]


class MemoryCacheStore:
    """In-process dict backend — handy for tests and single-process runs that don't
    want disk. Not shared across processes (so not for the dispatch path).
    """

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._d.get(key)

    def set(self, key: str, value: str) -> None:
        self._d[key] = value

    def exists(self, key: str) -> bool:
        return key in self._d

    def delete(self, key: str) -> None:
        self._d.pop(key, None)

    def iter_keys(self, prefix: str = "") -> Iterable[str]:
        return [k for k in self._d if k.startswith(prefix)]


class SqliteCacheStore:
    """Single-file SQLite backend (stdlib ``sqlite3`` — no extra dependency). One row per
    key in a ``cache(key, value)`` table; the DB is a single ``.db`` file, so it's a durable
    single-host cache without standing up Redis. Each op opens its own short-lived connection,
    so it's safe across the dispatch thread pool (SQLite's file locking serialises writers).

    Right for a single-host service or a CLI run that wants the cache to survive a restart;
    NOT for heavy multi-instance write contention across hosts (use Redis/SQL there).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

    def get(self, key: str) -> str | None:
        with closing(sqlite3.connect(self.path)) as connection:
            row: tuple[str] | None = connection.execute(
                "SELECT value FROM cache WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row is not None else None

    def set(self, key: str, value: str) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "INSERT INTO cache (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def exists(self, key: str) -> bool:
        with closing(sqlite3.connect(self.path)) as connection:
            row: tuple[int] | None = connection.execute(
                "SELECT 1 FROM cache WHERE key = ?", (key,)
            ).fetchone()
        return row is not None

    def delete(self, key: str) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DELETE FROM cache WHERE key = ?", (key,))

    def iter_keys(self, prefix: str = "") -> Iterable[str]:
        with closing(sqlite3.connect(self.path)) as connection:
            rows: list[tuple[str]] = connection.execute(
                "SELECT key FROM cache ORDER BY key"
            ).fetchall()
        return [row[0] for row in rows if row[0].startswith(prefix)]


class SqlCacheStore:
    """SQL backend via SQLAlchemy — works against any URL it supports (``postgresql://…``,
    ``mysql://…``, ``sqlite:///path.db``), so the DAG cache can live in the same database the
    rest of a deployed service already uses. ``sqlalchemy`` is a lazily-imported optional
    dependency.

    One ``key/value`` table (name configurable). ``set`` overwrites via delete-then-insert in
    one transaction — portable across dialects without a dialect-specific UPSERT. Pass a URL
    (``SqlCacheStore(url="postgresql://…")``) or an existing engine (``SqlCacheStore(engine=…)``)
    to share a connection pool with the host app.
    """

    def __init__(
        self, *, url: str | None = None, engine: Engine | None = None, table_name: str = "dag_cache"
    ) -> None:
        try:
            from sqlalchemy import Column, MetaData, String, Table, create_engine
        except ImportError as exc:  # pragma: no cover - exercised only without sqlalchemy installed
            raise ImportError(
                "SqlCacheStore needs the 'sqlalchemy' package: pip install sqlalchemy"
            ) from exc
        if engine is None:
            if url is None:
                raise ValueError("SqlCacheStore requires either 'url' or 'engine'")
            engine = create_engine(url)
        self._engine: Engine = engine
        self._table = Table(
            table_name,
            MetaData(),
            Column("key", String, primary_key=True),
            Column("value", String, nullable=False),
        )
        self._table.metadata.create_all(engine)

    def get(self, key: str) -> str | None:
        from sqlalchemy import select

        with self._engine.connect() as connection:
            value: str | None = connection.execute(
                select(self._table.c.value).where(self._table.c.key == key)
            ).scalar_one_or_none()
        return value

    def set(self, key: str, value: str) -> None:
        from sqlalchemy import delete, insert

        with self._engine.begin() as connection:
            connection.execute(delete(self._table).where(self._table.c.key == key))
            connection.execute(insert(self._table).values(key=key, value=value))

    def exists(self, key: str) -> bool:
        from sqlalchemy import select

        with self._engine.connect() as connection:
            row = connection.execute(
                select(self._table.c.key).where(self._table.c.key == key)
            ).first()
        return row is not None

    def delete(self, key: str) -> None:
        from sqlalchemy import delete

        with self._engine.begin() as connection:
            connection.execute(delete(self._table).where(self._table.c.key == key))

    def iter_keys(self, prefix: str = "") -> Iterable[str]:
        from sqlalchemy import select

        with self._engine.connect() as connection:
            keys: list[str] = list(
                connection.execute(select(self._table.c.key).order_by(self._table.c.key))
                .scalars()
                .all()
            )
        return [key for key in keys if key.startswith(prefix)]
