"""Tests for the atomic-write helpers in adag.io (atomic_writer, atomic_write_text)."""

from pathlib import Path

import pytest

from adag.io import atomic_write_text, atomic_writer


def test_atomic_write_text_writes_new_content(tmp_path: Path):
    """Happy path: new content lands at the target path."""
    target = tmp_path / "data.txt"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"


def test_atomic_write_text_replaces_existing_file(tmp_path: Path):
    """Existing target file is replaced with new content."""
    target = tmp_path / "data.txt"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_atomic_write_text_creates_missing_parent_directories(tmp_path: Path):
    """Missing parent directories are created on the way in."""
    target = tmp_path / "a" / "b" / "data.txt"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"


def test_atomic_write_text_leaves_no_temp_file_on_success(tmp_path: Path):
    """After a successful write the only file in the directory is the target."""
    target = tmp_path / "data.txt"
    atomic_write_text(target, "hello")
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_text_leaves_target_untouched_when_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If the rename step raises, the target file's previous content is preserved and no stale temp is left behind."""
    target = tmp_path / "data.txt"
    target.write_text("old content")

    def raising_replace(src: str, dst: str) -> None:
        raise OSError("simulated crash between write and rename")

    monkeypatch.setattr("os.replace", raising_replace)

    with pytest.raises(OSError, match="simulated crash"):
        atomic_write_text(target, "new content")

    assert target.read_text() == "old content"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_writer_supports_streaming_multiple_writes(tmp_path: Path):
    """The context-manager form supports iterative writes for large outputs (e.g. JSONL)."""
    target = tmp_path / "data.jsonl"
    with atomic_writer(target) as handle:
        for i in range(5):
            handle.write(f"line {i}\n")
    assert target.read_text() == "line 0\nline 1\nline 2\nline 3\nline 4\n"
