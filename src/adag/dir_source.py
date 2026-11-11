"""DirSource — a Deferred leaf node hashed by the contents of a directory.

Sibling of ``FileSource`` for cases where a Step's input is a folder of
files (e.g. a folder of accumulating note suggestions). Hashing the
folder content means adding, removing, or editing any file in the folder
invalidates the downstream cache automatically — the same property
``FileSource`` gives for a single file.

Resolve semantics: returns the folder Path as a string. Steps load the
files themselves via their own glob/parse logic; ``DirSource`` is only
responsible for content-aware cache keying.
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
    ``pattern`` inside the folder (recursive). If the folder doesn't
    exist, the hash returns ``None``, blocking any downstream step that
    depends on it.

    ``pattern`` defaults to ``"*.jsonl"`` — the common case for a folder
    of JSONL suggestion / overlay / config files. Pass another glob (e.g.
    ``"**/*.json"``) for nested layouts.
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
