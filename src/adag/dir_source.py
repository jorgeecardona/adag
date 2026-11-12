"""DirSource: a Deferred leaf hashed by the contents of a directory.

A sibling of ``FileSource`` for a step whose input is a folder of files. Adding, removing or
editing a file in the folder changes the hash, so the downstream cache recomputes.

``resolve`` returns the folder path as a string. The step reads the files itself. ``DirSource``
only keys the cache by content.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from adag.deferred import Deferred, ResolveContext
from adag.hashing import file_hash


@dataclass(frozen=True)
class DirSource(Deferred[str]):
    """A folder that acts as a leaf node in the step graph.

    Hashed by the SHA-256 of a deterministic stream of
    ``(relative_path, file_hash)`` pairs across every file matching
    ``pattern`` inside the folder. If the folder doesn't
    exist, the hash returns ``None``, blocking any downstream step that
    depends on it.

    ``pattern`` defaults to ``"*.jsonl"``, which is not recursive. Pass another glob (for
    example ``"**/*.json"``) for nested folders.
    """

    path: str
    pattern: str = "*.jsonl"

    @property
    def _path(self) -> Path:
        return Path(self.path)

    def _list_files(self) -> list[Path]:
        """All matching files, in stable sorted order by relative path."""
        if not self._path.exists() or not self._path.is_dir():
            return []
        return sorted(
            self._path.glob(self.pattern), key=lambda p: p.relative_to(self._path).as_posix()
        )

    def content_hash(self) -> str | None:
        """SHA-256 over ``(relative_path, file_hash)`` for each file.

        First 12 hex chars to match ``FileSource``. Returns ``None`` if
        the directory doesn't exist (treats as blocked).
        """
        if not self._path.exists() or not self._path.is_dir():
            return None
        h = hashlib.sha256()
        for p in self._list_files():
            rel = p.relative_to(self._path).as_posix()
            h.update(rel.encode())
            h.update(b"|")
            fh = file_hash(p)
            h.update(fh.encode())
            h.update(b"\n")
        return h.hexdigest()[:12]

    def resolve(self, context: ResolveContext) -> str:
        """Return the folder path. Steps load files themselves."""
        del context
        return str(self._path)
