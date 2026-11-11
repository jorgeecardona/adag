"""Content-addressable hashing utilities for the DAG engine."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePath
from typing import Any, TypeGuard, cast
from uuid import UUID

import structlog
from pydantic import BaseModel

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
        from adag.deferred import emit  # deferred imports this module; import late

        emit("hashing.large_file", path=str(path), size_mb=round(stat.st_size / 1_000_000, 1))

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


class UnhashableValueError(TypeError):
    """A value has a type the cache key cannot encode."""


def _frame(tag: bytes, payload: bytes) -> bytes:
    """Prefix ``payload`` with a type tag and its length so no two values share bytes."""
    return tag + len(payload).to_bytes(8, "big") + payload


def encode_value(value: Any, where: str = "value") -> bytes:
    """Return the cache-key bytes for one value.

    Every value is framed with a type tag and a byte length, so ``(1, 23)`` and
    ``(12, 3)``, or ``1`` and ``"1"``, never encode the same. A ``Path`` is hashed by
    its text, not its file content: use ``FileSource`` to hash content.

    Raises:
        UnhashableValueError: ``value`` has no stable encoding. ``where`` names it in
            the message. Wrap execution-only values in ``Config(...)``.
    """
    if value is None:
        return _frame(b"N", b"")
    if isinstance(value, bool):
        return _frame(b"B", b"1" if value else b"0")
    if isinstance(value, Enum):
        cls = type(value)
        name = f"{cls.__module__}.{cls.__qualname__}".encode()
        return _frame(b"E", _frame(b"S", name) + encode_value(value.value, where))
    if isinstance(value, int):
        return _frame(b"I", str(value).encode())
    if isinstance(value, float):
        return _frame(b"F", repr(value).encode())
    if isinstance(value, str):
        return _frame(b"S", value.encode())
    if isinstance(value, bytes):
        return _frame(b"Y", value)
    if isinstance(value, UUID):
        return _frame(b"Q", str(value).encode())
    if isinstance(value, PurePath):
        return _frame(b"P", value.as_posix().encode())
    if isinstance(value, type):
        return _frame(b"T", f"{value.__module__}.{value.__qualname__}".encode())
    if isinstance(value, BaseModel):
        cls = type(value)
        dumped = json.dumps(value.model_dump(mode="json"), sort_keys=True).encode()
        return _frame(b"M", f"{cls.__module__}.{cls.__qualname__}".encode() + b"\0" + dumped)
    return _encode_container(value, where)


def _encode_container(value: Any, where: str) -> bytes:
    """Encode a list, tuple, dict, set, or frozenset (the tail of ``encode_value``)."""
    # A list and a tuple with the same items encode the same: frozen dataclasses use
    # tuples where other code uses lists, and that choice should not change a key.
    if is_list(value) or is_tuple(value):
        return _frame(b"L", b"".join(encode_value(v, f"{where}[]") for v in value))
    if isinstance(value, dict):
        items = cast("dict[Any, Any]", value).items()
        pairs = sorted(
            (encode_value(k, f"{where} key"), encode_value(v, f"{where}[{k!r}]")) for k, v in items
        )
        return _frame(b"D", b"".join(k + v for k, v in pairs))
    if isinstance(value, (set, frozenset)):
        members = cast("set[Any] | frozenset[Any]", value)
        return _frame(b"Z", b"".join(sorted(encode_value(v, f"{where}{{}}") for v in members)))
    raise UnhashableValueError(
        f"{where} has type {type(value).__name__}, which the cache key cannot hash. "
        "Supported: None, bool, int, float, str, bytes, Enum, Path, UUID, type, BaseModel, "
        "and list / tuple / dict / set of those. Wrap execution-only values in Config(...)."
    )


def class_source_hash(cls: type) -> bytes | None:
    """Return a short hash of the class's own source code, or None if it has none.

    Only the source of the class body is hashed: the first class in the MRO whose
    source ``inspect`` can read. Edits to a base class or to a helper function the
    step calls do NOT change the hash. Bump a field or ``kind`` version when such an
    edit changes results. Any edit inside the class body, comments included, does.

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
