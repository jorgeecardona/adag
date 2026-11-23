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
    """File identity for caching: same fingerprint means same bytes.

    A cheap stand-in for re-reading. Inode and device are part of the identity
    because mtime resolution on many filesystems is 1 second, and an atomic replace
    inside the same second produces a new inode but identical (path, mtime, size):
    without ``st_ino`` the cache would return the stale hash. Frozen, so hashable and
    usable as a dict key.
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
# so without this cache a deep DAG re-tokenizes source thousands of times, the
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


KEY_FORMAT = 2
"""Version of the cache key bytes. It changes only when a release changes every key on purpose.

1 is the first release; 2 is the framed encoding from 0.2.0. ``tests/test_key_golden.py`` pins the
current format.
"""


class UnhashableValueError(TypeError):
    """A value has a type the cache key cannot encode."""


class TemplateInputError(TypeError):
    """A ``Template`` build function used the value of one of its inputs."""


class Placeholder:
    """A stand-in for a value that does not exist yet. ``Template`` uses it to hash its graph.

    A placeholder has a name and nothing else. It can be passed on, as a step field or an input,
    and the cache key encodes it by name. Any other use raises ``TemplateInputError``: reading an
    attribute, comparing, iterating, formatting, doing arithmetic. A build function that looks
    at the value would give a template hash that is wrong for the real values.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "name", name)

    def __repr__(self) -> str:
        return f"<template input {self.name!r}>"

    def __getattr__(self, attribute: str) -> Any:
        raise _used(self.name, f".{attribute}")


def _used(name: str, how: str) -> TemplateInputError:
    return TemplateInputError(
        f"template input {name!r} was used as a value ({how}). A template is built once, with "
        "placeholders, to hash its graph. Pass the input to a step as a field or an input and let "
        "the step read it."
    )


def _blocked(how: str) -> Any:
    def method(self: Placeholder, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise _used(self.name, how)

    return method


_BLOCKED_OPERATIONS = (
    "__bool__ __len__ __iter__ __next__ __getitem__ __setitem__ __delitem__ __contains__ "
    "__call__ __eq__ __ne__ __lt__ __le__ __gt__ __ge__ __hash__ __str__ __format__ __bytes__ "
    "__int__ __float__ __index__ __add__ __radd__ __sub__ __rsub__ __mul__ __rmul__ "
    "__truediv__ __rtruediv__ __floordiv__ __mod__ __matmul__ __neg__ __pos__ __abs__ "
    "__and__ __or__ __xor__ __invert__ __enter__ __exit__ __await__"
)
for _operation in _BLOCKED_OPERATIONS.split():
    setattr(Placeholder, _operation, _blocked(_operation))


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
            the message. Wrap execution-only values in ``Config(...)``; pass a graph node
            (a ``StepResult``) as a call input instead.
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
    if isinstance(value, Placeholder):
        return _frame(b"X", value.name.encode())
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
    # A graph node (StepResult, InputParam, any Deferred) is an edge, not a value. Config(...)
    # would hide the edge from the graph, so do not suggest it for one.
    if callable(getattr(value, "resolve", None)):
        advice = "It is a graph node: pass it as a call input, step(name=node), not as a field."
    else:
        advice = "Wrap execution-only values in Config(...)."
    raise UnhashableValueError(
        f"{where} has type {type(value).__name__}, which the cache key cannot hash. "
        "Supported: None, bool, int, float, str, bytes, Enum, Path, UUID, type, BaseModel, "
        f"and list / tuple / dict / set of those. {advice}"
    )


def hash_scalar(value: Any) -> bytes:
    """Return ``json.dumps(value, sort_keys=True)`` as bytes, for building your own item keys.

    Not used by adag's own cache keys (those use ``encode_value``). The bytes are the same as in
    the first release, so keys built with it stay valid.
    """
    return json.dumps(value, sort_keys=True).encode()


def hash_list(value: list[Any]) -> bytes:
    """Return ``json.dumps(value, sort_keys=True)`` as bytes. Same bytes as ``hash_scalar``."""
    return json.dumps(value, sort_keys=True).encode()


def class_source_hash(cls: type) -> bytes | None:
    """Return a short hash of the class's own source code, or None if it has none.

    Only the source of the class body is hashed: the first class in the MRO whose
    source ``inspect`` can read. Edits to a base class or to a helper function the
    step calls do NOT change the hash. Bump a field or ``kind`` version when such an
    edit changes results. Any edit inside the class body, comments included, does. To keep a key
    through such an edit, set ``code_version`` on the class (its text replaces this digest).

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
