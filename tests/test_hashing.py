"""Tests for hashing utilities: file_hash session cache, class_source_hash, type guards."""

from pathlib import Path
from typing import Any

from adag.hashing import (
    _file_hash_cache,
    class_source_hash,
    file_hash,
    hash_list,
    hash_scalar,
    is_dict,
    is_list,
)


def test_file_hash_changes_when_content_changes(tmp_path: Path):
    """Different bytes → different hash, regardless of mtime/size coincidence."""
    _file_hash_cache.clear()
    path = tmp_path / "data.txt"
    path.write_text("alpha")
    first = file_hash(path)
    path.write_text("beta!")  # same length, different bytes
    _file_hash_cache.clear()
    assert file_hash(path) != first


def test_file_hash_cache_invalidates_on_inode_change(tmp_path: Path, monkeypatch: Any):
    """Atomic replace within the same second creates a new inode; the cache key includes
    st_ino so the second read picks up the new content even if (path, mtime, size) match.

    Simulated here by pinning mtime/size constant across two writes and confirming the
    second call still returns the new hash (because st_ino changes on rewrite).
    """
    _file_hash_cache.clear()
    path = tmp_path / "data.txt"
    path.write_text("12345")

    real_stat = Path.stat
    pinned_mtime = path.stat().st_mtime
    pinned_size = path.stat().st_size

    def stat_with_pinned_mtime(self: Path, **kwargs: Any) -> Any:
        result = real_stat(self, **kwargs)
        # Cannot mutate os.stat_result; build a tiny stand-in exposing the same fields.

        class _Pinned:
            st_dev = result.st_dev
            st_ino = result.st_ino
            st_mtime = pinned_mtime
            st_size = pinned_size

        return _Pinned()

    monkeypatch.setattr(Path, "stat", stat_with_pinned_mtime)

    first = file_hash(path)
    # Replace the file with different bytes via an atomic rename so the target
    # gets a fresh inode (the test's whole point). A plain unlink+write_text
    # can reuse the just-freed inode on ext4 and break the simulation.
    replacement = tmp_path / "data.txt.new"
    replacement.write_text("67890")
    replacement.replace(path)
    second = file_hash(path)

    assert first != second


def test_is_list_true_for_list():
    assert is_list([1, 2, 3]) is True


def test_is_list_false_for_tuple():
    assert is_list((1, 2)) is False


def test_is_dict_true_for_dict():
    assert is_dict({"a": 1}) is True


def test_is_dict_false_for_list():
    assert is_dict([1, 2]) is False


def test_hash_scalar_is_deterministic():
    assert hash_scalar("x") == hash_scalar("x")


def test_hash_list_is_deterministic():
    assert hash_list([1, 2, 3]) == hash_list([1, 2, 3])


def test_class_source_hash_returns_bytes_for_inspectable_class():
    class Sample:
        def run(self) -> int:
            return 1

    result = class_source_hash(Sample)
    assert result is not None
    assert isinstance(result, bytes)


def test_class_source_hash_stable_across_calls():
    class Sample:
        pass

    assert class_source_hash(Sample) == class_source_hash(Sample)
