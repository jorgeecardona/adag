"""Sink steps — terminal nodes that write a pipeline result to disk.

These are plain ``Step``s (not ``CachedStep``s) because their value is the
side-effect, not the return value. A regular Step re-runs on every
``pipeline.execute()`` call — including on upstream cache hits — which is
exactly what you want for a "publish to disk" node: if someone deletes the
output file, the next run restores it without invalidating the upstream
cache.

Wire them at the end of a pipeline::

    notes = GenerateNotes(...)(tiers=tiers)
    sink = WriteJSONL(path="output/notes.jsonl")(source=notes)

    pipeline = Pipeline(sink, cache_dir=cache_dir)
    pipeline.execute()  # writes notes.jsonl every run
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from adag.hashing import is_list
from adag.io import atomic_writer
from adag.step import Step


def _as_basemodel_list(value: Any) -> list[BaseModel] | None:
    """Return ``value`` as a non-empty ``list[BaseModel]``, or ``None``.

    Uses the package's ``is_list`` TypeGuard so the element type is
    ``Any`` (not ``Unknown``) inside the loop.
    """
    if not is_list(value):
        return None
    if not value:
        return None
    out: list[BaseModel] = []
    for item in value:
        if not isinstance(item, BaseModel):
            return None
        out.append(item)
    return out


@dataclass(frozen=True)
class WriteJSONL(Step[str]):
    """Write the single ``list[BaseModel]`` field of a Pydantic result as JSONL.

    Reads the upstream Pydantic model, finds its lone ``list[BaseModel]``
    field, and writes one record per line to ``path``. Returns ``path``
    so callers can chain a downstream ``FileSource`` if they want.

    ``source`` is typed ``BaseModel``: ``StepResult._validate`` is
    Liskov-aware (accepts any subclass), so a concrete Pydantic result
    type wires in directly without dropping to ``Any`` at the call site.

    Raises ``TypeError`` if the upstream result has zero or more than one
    ``list[BaseModel]`` fields.
    """

    kind = "adag/write_jsonl/v1"

    path: str

    def run(self, source: BaseModel, **_: Any) -> str:
        list_fields: list[tuple[str, list[BaseModel]]] = []
        for name in source.__class__.model_fields:
            records = _as_basemodel_list(getattr(source, name))
            if records is not None:
                list_fields.append((name, records))

        if len(list_fields) != 1:
            found = [name for name, _ in list_fields]
            raise TypeError(
                f"WriteJSONL requires the source to have exactly one non-empty "
                f"list[BaseModel] field; {type(source).__name__} has "
                f"{len(list_fields)} (found: {found})."
            )

        _field_name, records = list_fields[0]
        del _field_name
        target = Path(self.path)
        with atomic_writer(target) as f:
            for record in records:
                f.write(record.model_dump_json() + "\n")
        return self.path
