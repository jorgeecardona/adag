"""Tests for WriteJSONL — the terminal sink step that writes a result to disk."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import Pipeline, Step, WriteJSONL


class Record(BaseModel):
    code: str
    text: str


class Notes(BaseModel):
    notes: list[Record]
    n: int


@dataclass(frozen=True)
class GenerateNotes(Step[Notes]):
    """Produces a fixed set of records — stand-in for the real LLM step."""

    n: int = 3

    def run(self, n: int) -> Notes:
        records = [Record(code=f"C{i}", text=f"text-{i}") for i in range(n)]
        return Notes(notes=records, n=len(records))


def test_writes_jsonl_one_record_per_line(tmp_path: Path):
    """The sink writes exactly one JSON object per line."""
    output = tmp_path / "notes.jsonl"
    notes_step = GenerateNotes(n=3)
    sink = WriteJSONL(path=str(output))(source=notes_step())

    pipeline = Pipeline(sink, cache_dir=tmp_path / "cache")
    returned = pipeline.execute()

    assert returned == str(output)
    lines = output.read_text().splitlines()
    assert len(lines) == 3
    assert all(line.startswith("{") and line.endswith("}") for line in lines)


def test_creates_parent_directory(tmp_path: Path):
    """A missing parent directory is created automatically."""
    output = tmp_path / "deeply" / "nested" / "out.jsonl"
    notes_step = GenerateNotes(n=2)
    sink = WriteJSONL(path=str(output))(source=notes_step())

    Pipeline(sink, cache_dir=tmp_path / "cache").execute()

    assert output.exists()
    assert len(output.read_text().splitlines()) == 2


def test_fires_on_upstream_cache_hit(tmp_path: Path):
    """Deleting the output file and re-running restores it from a cache hit."""
    output = tmp_path / "notes.jsonl"
    notes_step = GenerateNotes(n=2)
    sink = WriteJSONL(path=str(output))(source=notes_step())
    pipeline = Pipeline(sink, cache_dir=tmp_path / "cache")

    pipeline.execute()
    assert output.exists()

    output.unlink()
    assert not output.exists()

    # Second run: upstream is a cache hit, sink still re-runs and re-writes.
    pipeline.execute()
    assert output.exists()
    assert len(output.read_text().splitlines()) == 2


def test_raises_when_no_list_field(tmp_path: Path):
    """A source with no list[BaseModel] field is an unambiguous error."""

    class Scalar(BaseModel):
        value: str

    @dataclass(frozen=True)
    class ScalarStep(Step[Scalar]):
        def run(self) -> Scalar:
            return Scalar(value="x")

    sink = WriteJSONL(path=str(tmp_path / "out.jsonl"))(source=ScalarStep()())
    with pytest.raises(TypeError, match="exactly one"):
        Pipeline(sink).execute()


def test_raises_when_multiple_list_fields(tmp_path: Path):
    """A source with two list[BaseModel] fields can't be disambiguated."""

    class TwoLists(BaseModel):
        a: list[Record]
        b: list[Record]

    @dataclass(frozen=True)
    class TwoListsStep(Step[TwoLists]):
        def run(self) -> TwoLists:
            return TwoLists(a=[Record(code="A", text="a")], b=[Record(code="B", text="b")])

    sink = WriteJSONL(path=str(tmp_path / "out.jsonl"))(source=TwoListsStep()())
    with pytest.raises(TypeError, match="exactly one"):
        Pipeline(sink).execute()


def test_path_is_part_of_content_hash():
    """Two sinks pointing at different paths are different steps (different hash)."""
    a = WriteJSONL(path="output/a.jsonl")
    b = WriteJSONL(path="output/b.jsonl")
    assert a.content_hash() != b.content_hash()
