"""Filesystem I/O for the DAG: leaf Deferreds that read, helpers that write.

Read leaves (content-addressable Deferreds: each hashes by the file's bytes so caches
invalidate when the file changes; a missing file hashes to ``None``, which blocks the key of a
step that reads the source, as a field or as an input):

  - ``FileSource(path)`` → ``Deferred[str]``: the file's complete text.
  - ``LinesSource(path)`` → ``Deferred[list[str]]``: raw line-by-line (blank
    lines preserved; line endings stripped, as per ``str.splitlines()``).
  - ``JSONLSource(path, record_type=Record)`` → ``Deferred[list[Record]]``:
    each non-blank line parsed as JSON and validated against the Pydantic
    record type. ``record_type`` is required: typing is the whole point of
    this leaf, and a "raw dicts" variant would erase the per-line schema.
    If you want untyped lines, use ``LinesSource`` and ``json.loads`` in a
    downstream Step.

Write helpers (atomic via temp-file + ``os.replace``; readers see either the
complete previous content or the complete new content, never a partial
write):

  - ``atomic_writer(path)``: context manager yielding a text-mode handle.
  - ``atomic_write_text(path, text)``: one-shot wrapper for short strings.

The atomic-write idiom is used by every cache and manifest in this package
(``ItemCache``, ``Pipeline.save``, ``Step._save``, ``WriteJSONL``) so that a
crash mid-write (Lambda timeout, OOM, SIGKILL) can't leave half-written
JSON on disk.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TextIO, TypeVar

from pydantic import BaseModel

from adag.deferred import Deferred, ResolveContext
from adag.hashing import file_hash

# Read side: content-addressable leaf Deferreds


def _existing_path_hash(path: str | os.PathLike[str], source: str) -> str | None:
    """Hash the file at ``path`` by content, or ``None`` if it doesn't exist.

    Shared by every read-leaf below so the hashing convention stays in one
    place; downstream caches invalidate consistently regardless of which
    leaf type produced the bytes.

    Raises:
        IsADirectoryError: ``path`` is a directory (an empty path is the working directory).
            The message names the source class and the path. Use ``DirSource`` for a folder.
    """
    p = Path(path)
    if p.is_dir():
        raise IsADirectoryError(
            f"{source}(path={path!r}) points at a directory ({p.resolve()}), not a file. "
            "Use DirSource for a folder."
        )
    return file_hash(p) if p.exists() else None


@dataclass(frozen=True)
class FileSource(Deferred[str]):
    """Reads the file's full text content into a single string."""

    path: str | os.PathLike[str]

    def __post_init__(self) -> None:
        # Store a str whatever was passed, so equality and the cache key do not depend on the type (#16).
        object.__setattr__(self, "path", os.fspath(self.path))

    def content_hash(self) -> str | None:
        return _existing_path_hash(self.path, type(self).__name__)

    def resolve(self, context: ResolveContext) -> str:
        del context  # leaf source: no cache_dir or params consulted
        return Path(self.path).read_text()


@dataclass(frozen=True)
class LinesSource(Deferred[list[str]]):
    """Reads the file as a list of lines (raw, line endings stripped).

    Blank lines are preserved: this is the literal contents of the file split
    on newlines, equivalent to ``Path(path).read_text().splitlines()``. Use
    ``JSONLSource`` if you want blanks skipped and each line parsed.
    """

    path: str | os.PathLike[str]

    def __post_init__(self) -> None:
        # Store a str whatever was passed, so equality and the cache key do not depend on the type (#16).
        object.__setattr__(self, "path", os.fspath(self.path))

    def content_hash(self) -> str | None:
        return _existing_path_hash(self.path, type(self).__name__)

    def resolve(self, context: ResolveContext) -> list[str]:
        del context  # leaf source: no cache_dir or params consulted
        return Path(self.path).read_text().splitlines()


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class JSONLSource(Deferred[list[T]], Generic[T]):
    """Reads a JSONL file and validates each non-blank line against ``record_type``.

    ``record_type`` is required (not optional). The Pydantic class is what
    gives this leaf its precise output type. Making it optional would force
    a ``list[T] | list[Any]`` return signature and erase the typing benefit.
    If you want untyped lines, use ``LinesSource`` and call ``json.loads`` in
    a downstream Step.

    Usage::

        source = JSONLSource(path="data/reports.jsonl", record_type=ReportEntry)
        records: list[ReportEntry] = source.resolve(context)

    A line that fails to parse or validate raises ``ValueError`` with the file
    path and line number: the original error is chained via ``raise … from``.
    Blank lines are skipped silently.
    """

    path: str | os.PathLike[str]
    record_type: type[T]

    def __post_init__(self) -> None:
        # Store a str whatever was passed, so equality and the cache key do not depend on the type (#16).
        object.__setattr__(self, "path", os.fspath(self.path))

    def content_hash(self) -> str | None:
        return _existing_path_hash(self.path, type(self).__name__)

    def resolve(self, context: ResolveContext) -> list[T]:
        del context  # leaf source: no cache_dir or params consulted
        records: list[T] = []
        for line_number, line in enumerate(Path(self.path).read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(self.record_type.model_validate_json(stripped))
            except Exception as error:
                raise ValueError(f"{self.path}:{line_number}: {error}") from error
        return records


# Write side: crash-safe atomic writes


@contextmanager
def atomic_writer(path: Path) -> Generator[TextIO]:
    """Open ``path`` for atomic text writing. Yields a text-mode file handle.

    On normal context exit, the bytes are flushed, fsynced, and the temp file
    is renamed over ``path`` in a single inode operation. If the caller's
    block raises (or the process is killed before the rename), the temp file
    is unlinked and ``path`` is left untouched, readers continue to see the
    previous content.

    The unique tempfile name (from ``tempfile.mkstemp``) also makes concurrent
    writers to the same target safe: each gets its own temp file, and whichever
    finishes its rename last wins cleanly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    renamed = False
    try:
        with os.fdopen(fd, "w") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        renamed = True
    finally:
        if not renamed:
            tmp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """One-shot atomic text write. Equivalent to ``Path.write_text`` but crash-safe."""
    with atomic_writer(path) as handle:
        handle.write(text)
