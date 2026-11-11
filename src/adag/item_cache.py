"""ItemCache — per-item cache for expensive batch operations.

Designed for LLM calls where each item costs money. Saves results
per-item so that a crash at item 1500/2000 only loses the last batch,
not the first 1500.

The ItemCache gets its cache directory from the Pipeline that creates it.

Storage is delegated to a ``CacheStore`` (the pluggable key->text backend) so the same per-item cache can
live on the local filesystem (default), in Redis/Valkey (shared, for a deployed service), or anywhere else —
without ItemCache knowing or caring. ItemCache owns only the higher-level concerns, expressed PURELY as
key->text operations:

1. **Schema-version stamp** — every saved value carries the schema's
   ``model_json_schema()`` digest. On load, a mismatch is reported and the
   entry is treated as stale (forces re-processing). Prevents silent
   corruption when a schema field is added/renamed/retyped and old caches
   would otherwise deserialize wrong.

2. **In-flight WAL** — ``mark_inflight(key)`` writes a ``<key>.inflight``
   sentinel (just another store key) before processing starts.
   ``clear_inflight(key)`` removes it after success. ``inflight_keys()``
   lists any sentinels left behind by a SIGTERM-killed process so callers
   can surface them in resume logs.

The default ``FileSystemCacheStore`` is rooted at ``<cache_dir>/.items`` and keys are ``<namespace>/<key>``,
so the on-disk layout (``<cache_dir>/.items/<namespace>/<key>.json``) is byte-for-byte what it has always
been — existing caches load unchanged.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Generic, TypeVar

import structlog
from pydantic import BaseModel

from adag.cache_store import CacheStore, FileSystemCacheStore
from adag.deferred import emit
from adag.hashing import is_dict

logger = structlog.get_logger()

R = TypeVar("R", bound=BaseModel)


_SCHEMA_VERSION_FIELD = "_schema_version"
_DATA_FIELD = "_data"


class ItemCache(Generic[R]):
    """Per-item cache for expensive batch operations.

    Items are stored as individual JSON values keyed by a content hash.
    This survives parameter changes — only the item's own identity matters.

    Usage::

        cache = ItemCache[NoteSchema](cache_dir, "note_generation", NoteSchema)

        keys = [cache.key(item.code, style_hash, model) for item in items]
        cached, uncached_indices = cache.partition(keys)

        for index in uncached_indices:
            result = expensive_llm_call(items[index])
            cache.save(keys[index], result)

        all_results = cache.collect(keys)

    Pass ``store=`` to back the cache with something other than the local filesystem (e.g. a
    ``RedisCacheStore`` for a deployed multi-instance service). Default = ``FileSystemCacheStore`` rooted at
    ``<cache_dir>/.items`` (the historical layout).
    """

    def __init__(
        self, cache_dir: Path, namespace: str, schema: type[R], *, store: CacheStore | None = None
    ) -> None:
        self.cache_dir = cache_dir
        self.namespace = namespace
        self.schema: type[R] = schema
        self.store: CacheStore = (
            store if store is not None else FileSystemCacheStore(Path(cache_dir).joinpath(".items"))
        )

    # Keys in the store are namespaced + suffixed so result files and in-flight markers coexist and the
    # default filesystem layout (<cache_dir>/.items/<namespace>/<key>.json) is preserved exactly.
    def _item_key(self, item_key: str) -> str:
        return f"{self.namespace}/{item_key}.json"

    def _inflight_key(self, item_key: str) -> str:
        return f"{self.namespace}/{item_key}.inflight"

    def key(self, *parts: str) -> str:
        """Compute a cache key from the item's identity parts."""
        h = hashlib.sha256()
        for part in parts:
            h.update(part.encode())
        return h.hexdigest()[:16]

    def _schema_version(self) -> str:
        """Stable digest of the schema's JSON-schema representation.

        Encodes the field names, types, requireds, and nesting. If the
        schema changes (field added, renamed, retyped) the digest changes,
        old caches become stale, and partition() treats them as misses.
        """
        spec = self.schema.model_json_schema()
        return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]

    def exists(self, item_key: str) -> bool:
        """Check if an item is cached."""
        return self.store.exists(self._item_key(item_key))

    # Alias — "has" reads more naturally for cache operations.
    has = exists

    def list_keys(self) -> set[str]:
        """Return all cached item keys."""
        prefix = f"{self.namespace}/"
        return {
            k[len(prefix) : -len(".json")]
            for k in self.store.iter_keys(prefix)
            if k.endswith(".json")
        }

    # ------------------------------------------------------------------
    # In-flight WAL
    # ------------------------------------------------------------------

    def mark_inflight(self, item_key: str) -> None:
        """Record that processing for ``item_key`` has started.

        Writes a ``<key>.inflight`` sentinel. If the worker crashes before
        ``save()``, this sentinel persists and can be surfaced on next
        resume to flag work-in-progress items that were lost. Call
        ``clear_inflight()`` after a successful save.
        """
        self.store.set(self._inflight_key(item_key), "")

    def clear_inflight(self, item_key: str) -> None:
        """Remove the in-flight sentinel after a successful save."""
        self.store.delete(self._inflight_key(item_key))

    def inflight_keys(self) -> set[str]:
        """All currently-marked in-flight item keys (typically from a crashed run)."""
        prefix = f"{self.namespace}/"
        return {
            k[len(prefix) : -len(".inflight")]
            for k in self.store.iter_keys(prefix)
            if k.endswith(".inflight")
        }

    # ------------------------------------------------------------------
    # Save / load with schema-version wrapping
    # ------------------------------------------------------------------

    def save(self, item_key: str, result: R) -> None:
        """Save a single item result with a schema-version stamp.

        Stamping the schema version on every write lets a future
        ``load()`` detect when the cached payload was produced under a
        different schema and refuse to deserialize it. Old, pre-stamp
        caches still load via the legacy path below for backwards
        compatibility.
        """
        wrapper: dict[str, Any] = {
            _SCHEMA_VERSION_FIELD: self._schema_version(),
            _DATA_FIELD: result.model_dump(mode="json"),
        }
        self.store.set(self._item_key(item_key), json.dumps(wrapper, indent=2))
        self.clear_inflight(item_key)

    def load(self, item_key: str) -> R:
        """Load a single item result, honoring the schema-version stamp.

        Three on-disk shapes are accepted:

        - **Stamped** (current): ``{"_schema_version": "...", "_data": {...}}``
          → version-checked. Mismatch raises ``ValueError``;
          ``partition()`` treats this as a cache miss and re-processes.
        - **Legacy** (pre-stamp): top-level Pydantic JSON
          → loaded via ``model_validate``; surfaces as a "stale" miss
          only if the schema fields no longer match.
        - **Corrupt**: invalid JSON or schema mismatch → raises;
          ``partition()`` catches this and treats as a miss.
        """
        text = self.store.get(self._item_key(item_key))
        if text is None:
            raise FileNotFoundError(f"cache miss for {item_key!r} in namespace {self.namespace!r}")
        raw: Any = json.loads(text)
        if is_dict(raw) and _SCHEMA_VERSION_FIELD in raw and _DATA_FIELD in raw:
            stored_version = raw[_SCHEMA_VERSION_FIELD]
            expected = self._schema_version()
            if stored_version != expected:
                raise ValueError(
                    f"schema_version mismatch for {item_key}: stored={stored_version} expected={expected}"
                )
            return self.schema.model_validate(raw[_DATA_FIELD])
        # Legacy path — older caches stored the Pydantic JSON directly.
        return self.schema.model_validate(raw)

    def partition(self, keys: list[str]) -> tuple[dict[str, R], list[int]]:
        """Split keys into cached results and uncached indices.

        If a cached item fails validation (e.g. schema changed), it is
        treated as a cache miss and will be re-processed. Any in-flight
        markers from a prior crashed run are also reported.
        """
        cached: dict[str, R] = {}
        uncached_indices: list[int] = []
        n_stale = 0
        for index, item_key in enumerate(keys):
            if self.exists(item_key):
                try:
                    cached[item_key] = self.load(item_key)
                except Exception as exc:
                    emit(
                        "item_cache.stale_entry",
                        namespace=self.namespace,
                        item_key=item_key,
                        error_type=type(exc).__name__,
                        error=str(exc)[:200],
                    )
                    uncached_indices.append(index)
                    n_stale += 1
            else:
                uncached_indices.append(index)

        n_inflight = len(self.inflight_keys())
        if cached or n_stale or n_inflight:
            emit(
                "item_cache.partition",
                namespace=self.namespace,
                cached=len(cached),
                uncached=len(uncached_indices),
                stale=n_stale,
                inflight_orphans=n_inflight,
            )

        return cached, uncached_indices

    def collect(self, keys: list[str], *, allow_missing: bool = False) -> list[R]:
        """Load all items by keys.

        Default behavior: raises ``ValueError`` if any key is missing — the
        downstream step probably can't proceed without a complete result set.

        With ``allow_missing=True``: silently skip missing keys and return
        whatever was actually cached. Used by batch steps that tolerate
        partial failure (e.g. LLM calls where a small percentage may
        legitimately fail due to rate limits / transient errors, but the
        successful majority is still worth keeping).
        """
        results: list[R] = []
        missing = 0
        for item_key in keys:
            if not self.exists(item_key):
                if allow_missing:
                    missing += 1
                    continue
                raise ValueError(f"Item '{item_key}' not found in cache '{self.namespace}'")
            results.append(self.load(item_key))
        if missing:
            emit(
                "item_cache.collect.partial",
                namespace=self.namespace,
                kept=len(results),
                missing=missing,
            )
        return results
