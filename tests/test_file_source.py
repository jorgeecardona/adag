"""Tests for the file leaf Deferreds: FileSource / LinesSource / JSONLSource."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from adag.deferred import ResolveContext
from adag.io import FileSource, JSONLSource, LinesSource

# Content-addressable hashing — shared across all three leaf types


def test_file_source_hash_content_based(tmp_path: Path):
    """Hash is determined by file content, not path."""
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("hello world")
    file_b.write_text("hello world")
    assert FileSource(str(file_a)).content_hash() == FileSource(str(file_b)).content_hash()


def test_file_source_hash_changes_with_content(tmp_path: Path):
    """Changing file content changes the hash."""
    path = tmp_path / "data.txt"
    path.write_text("version 1")
    hash_v1 = FileSource(str(path)).content_hash()
    path.write_text("version 2")
    # Clear session cache — mtime may not change fast enough on some filesystems.
    from adag.hashing import _file_hash_cache

    _file_hash_cache.clear()
    hash_v2 = FileSource(str(path)).content_hash()
    assert hash_v1 != hash_v2


def test_file_source_hash_none_when_missing():
    """Missing file returns None hash; downstream steps that depend on it are blocked."""
    assert FileSource("/nonexistent/file.txt").content_hash() is None


def test_lines_source_hash_matches_file_source(tmp_path: Path):
    """LinesSource and FileSource over the same path produce the same content hash —
    the hash identifies the underlying bytes, not the parse shape.
    """
    path = tmp_path / "data.txt"
    path.write_text("line 1\nline 2\n")
    assert LinesSource(str(path)).content_hash() == FileSource(str(path)).content_hash()


def test_jsonl_source_hash_matches_file_source(tmp_path: Path):
    """JSONLSource hashes the file bytes regardless of record_type."""

    class Record(BaseModel):
        x: int

    path = tmp_path / "data.jsonl"
    path.write_text('{"x": 1}\n')
    assert (
        JSONLSource(path=str(path), record_type=Record).content_hash()
        == FileSource(str(path)).content_hash()
    )


# FileSource — full text


def test_file_source_resolve_returns_full_text(tmp_path: Path):
    """resolve() returns the file's complete text as a single string."""
    path = tmp_path / "data.txt"
    path.write_text("hello world")
    assert FileSource(str(path)).resolve(ResolveContext()) == "hello world"


def test_file_source_resolve_preserves_newlines(tmp_path: Path):
    """resolve() does not strip line endings."""
    path = tmp_path / "data.txt"
    path.write_text("a\nb\n")
    assert FileSource(str(path)).resolve(ResolveContext()) == "a\nb\n"


# LinesSource — raw line-by-line


def test_lines_source_returns_lines_without_endings(tmp_path: Path):
    """Lines come back without their trailing newline character."""
    path = tmp_path / "data.txt"
    path.write_text("alpha\nbeta\ngamma\n")
    assert LinesSource(str(path)).resolve(ResolveContext()) == ["alpha", "beta", "gamma"]


def test_lines_source_preserves_blank_lines(tmp_path: Path):
    """Blank lines are kept — LinesSource is the literal split, not a filter."""
    path = tmp_path / "data.txt"
    path.write_text("a\n\nb\n")
    assert LinesSource(str(path)).resolve(ResolveContext()) == ["a", "", "b"]


def test_lines_source_handles_no_trailing_newline(tmp_path: Path):
    """A file without a trailing newline still yields the final line."""
    path = tmp_path / "data.txt"
    path.write_text("only")
    assert LinesSource(str(path)).resolve(ResolveContext()) == ["only"]


# JSONLSource — parsed + validated


def test_jsonl_source_parses_each_line(tmp_path: Path):
    """Each non-blank line is validated against record_type."""

    class Record(BaseModel):
        name: str
        value: int

    path = tmp_path / "records.jsonl"
    path.write_text('{"name": "a", "value": 1}\n{"name": "b", "value": 2}\n')
    records = JSONLSource(path=str(path), record_type=Record).resolve(ResolveContext())
    assert records == [Record(name="a", value=1), Record(name="b", value=2)]


def test_jsonl_source_skips_blank_lines(tmp_path: Path):
    """Blank or whitespace-only lines are skipped silently."""

    class Record(BaseModel):
        x: int

    path = tmp_path / "data.jsonl"
    path.write_text('{"x": 1}\n\n   \n{"x": 2}\n')
    records = JSONLSource(path=str(path), record_type=Record).resolve(ResolveContext())
    assert records == [Record(x=1), Record(x=2)]


def test_jsonl_source_validation_error_includes_line_number(tmp_path: Path):
    """A line that fails validation raises ValueError tagged with file:line — the original
    Pydantic ValidationError is chained as __cause__.
    """

    class Record(BaseModel):
        x: int

    path = tmp_path / "bad.jsonl"
    path.write_text('{"x": 1}\n{"x": "not_an_int"}\n')

    with pytest.raises(ValueError, match="bad.jsonl:2"):
        JSONLSource(path=str(path), record_type=Record).resolve(ResolveContext())


@pytest.mark.parametrize("path", ["", "."])
def test_file_source_on_a_directory_names_the_source(path: str) -> None:
    """A directory path raises an error naming the FileSource, not a bare OS error (#6)."""
    with pytest.raises(IsADirectoryError, match=r"FileSource\(path=.*DirSource"):
        FileSource(path).content_hash()


# A pathlib.Path is accepted and stored as str, so the hash and the cache key do not change (#16)


class _Record(BaseModel):
    n: int


def test_sources_accept_a_path_and_store_a_str(tmp_path: Path):
    from adag.dir_source import DirSource

    file = tmp_path / "data.jsonl"
    file.write_text('{"n": 1}\n')
    sources = [
        FileSource(file),
        LinesSource(file),
        JSONLSource(file, _Record),
        DirSource(tmp_path),
    ]
    assert [type(s.path) for s in sources] == [str, str, str, str]
    assert FileSource(file) == FileSource(str(file))
    assert FileSource(file).content_hash() == FileSource(str(file)).content_hash()
    assert JSONLSource(file, _Record).resolve(ResolveContext(cache_dir=tmp_path, params={})) == [
        _Record(n=1)
    ]
