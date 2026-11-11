"""Content-addressable hashing utilities for the DAG engine."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

import structlog

logger = structlog.get_logger()

_LARGE_FILE_BYTES = 1_000_000  # log an info line when hashing a file bigger than this


@dataclass(frozen=True)
class FileFingerprint:
    """Identity of a file's content for caching: same fingerprint ⇒ same bytes
    (cheap stand-in for re-reading). Inode + device are part of the identity
    because mtime resolution on many filesystems is 1s, and an atomic-replace
    inside the same second produces a new inode but identical (path, mtime,
    size) — without st_ino the cache would return the stale hash. Frozen ⇒
    hashable ⇒ usable as a dict key.
    """

    path: str
    dev: int
    inode: int
    mtime: float
    size: int


# Session-level cache: file fingerprint → hash.
# Avoids re-reading large files within the same process.
_file_hash_cache: dict[FileFingerprint, str] = {}

# Session-level cache: class → source hash. A class's source can't change at
# runtime, but inspect.getsource() re-reads AND re-tokenizes the source file on
# every call. content_hash() requests this once per step instance and the same
# step class appears many times across a graph (e.g. one per refinement cycle),
# so without this cache a deep DAG re-tokenizes source thousands of times — the
# dominant cost of the silent startup before the first step logs. Keyed by the
# class object (redefining a class yields a new object → fresh entry).
_class_source_hash_cache: dict[type, bytes | None] = {}


def file_hash(path: Path) -> str:
    """SHA-256 of file content, first 12 hex chars. Session-cached by file fingerprint."""
    resolved = path.resolve()
    stat = resolved.stat()
    cache_key = FileFingerprint(
        path=str(resolved),
        dev=stat.st_dev,
        inode=stat.st_ino,
        mtime=stat.st_mtime,
        size=stat.st_size,
    )
    cached = _file_hash_cache.get(cache_key)
    if cached is not None:
        return cached

    if stat.st_size > _LARGE_FILE_BYTES:
        logger.info(
            "hashing.large_file", path=str(path), size_mb=round(stat.st_size / 1_000_000, 1)
        )

    h = hashlib.sha256()
    with open(resolved, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    result = h.hexdigest()[:12]
    _file_hash_cache[cache_key] = result
    return result


def is_list(obj: Any) -> TypeGuard[list[Any]]:
    """Type guard for list[Any]. Use instead of isinstance to avoid list[Unknown]."""
    return isinstance(obj, list)


def is_tuple(obj: Any) -> TypeGuard[tuple[Any, ...]]:
    """Type guard for tuple[Any, ...]. Use instead of isinstance to avoid tuple[Unknown, ...]."""
    return isinstance(obj, tuple)


def is_dict(obj: Any) -> TypeGuard[dict[str, Any]]:
    """Type guard for dict[str, Any]. Use instead of isinstance to avoid dict[Unknown, Unknown]."""
    return isinstance(obj, dict)


def hash_scalar(value: Any) -> bytes:
    """Deterministic hash bytes for a scalar value."""
    return json.dumps(value, sort_keys=True).encode()


def hash_list(value: list[Any]) -> bytes:
    """Deterministic hash bytes for a list of scalars."""
    return json.dumps(value, sort_keys=True).encode()


def class_source_hash(cls: type) -> bytes | None:
    """SHA-256 of the run() method source code, for cache invalidation on implementation changes.

    Walks the MRO to find the first class with inspectable source.
    Returns None for dynamically generated classes without source.

    Memoized per class: inspect.getsource() re-reads + re-tokenizes the source
    file on every call, and this is invoked once per step instance across the
    whole graph, so caching turns a per-call file tokenization into a one-off.
    """
    if cls in _class_source_hash_cache:
        return _class_source_hash_cache[cls]
    result: bytes | None = None
    for klass in cls.__mro__:
        if klass is object:
            break
        try:
            source = inspect.getsource(klass)
            result = hashlib.sha256(source.encode()).hexdigest()[:12].encode()
            break
        except (OSError, TypeError):
            continue
    _class_source_hash_cache[cls] = result
    return result
