"""Tests for ItemCache — per-item disk cache."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from adag.item_cache import ItemCache


class NoteEntry(BaseModel):
    """Simple result schema for testing."""

    code: str
    note: str


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """Base directory for item cache."""
    return tmp_path / "data"


def test_key_deterministic(base: Path):
    """Same parts produce the same key."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    assert cache.key("A", "model-v1") == cache.key("A", "model-v1")


def test_key_changes_with_input(base: Path):
    """Different parts produce different keys."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    assert cache.key("A", "model-v1") != cache.key("B", "model-v1")


def test_save_and_load(base: Path):
    """Save and load roundtrips a Pydantic model."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    key = cache.key("test-code")
    entry = NoteEntry(code="test-code", note="Test note content")
    cache.save(key, entry)
    loaded = cache.load(key)
    assert loaded.code == "test-code"
    assert loaded.note == "Test note content"


def test_exists_false_before_save(base: Path):
    """Item does not exist before save."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    assert not cache.exists(cache.key("missing"))


def test_exists_true_after_save(base: Path):
    """Item exists after save."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    key = cache.key("saved")
    cache.save(key, NoteEntry(code="saved", note="content"))
    assert cache.exists(key)


def test_partition_all_uncached(base: Path):
    """All items are uncached when none have been saved."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    keys = [cache.key("a"), cache.key("b"), cache.key("c")]
    cached, uncached = cache.partition(keys)
    assert len(cached) == 0
    assert uncached == [0, 1, 2]


def test_partition_mixed(base: Path):
    """Partition correctly separates cached and uncached items."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    keys = [cache.key("a"), cache.key("b"), cache.key("c")]
    cache.save(keys[0], NoteEntry(code="a", note="cached"))
    cache.save(keys[2], NoteEntry(code="c", note="cached"))

    cached, uncached = cache.partition(keys)
    assert len(cached) == 2
    assert cached[keys[0]].code == "a"
    assert cached[keys[2]].code == "c"
    assert uncached == [1]


def test_collect_all(base: Path):
    """Collect returns all items in order."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    keys = [cache.key("x"), cache.key("y")]
    cache.save(keys[0], NoteEntry(code="x", note="first"))
    cache.save(keys[1], NoteEntry(code="y", note="second"))

    results = cache.collect(keys)
    assert len(results) == 2
    assert results[0].code == "x"
    assert results[1].code == "y"


def test_collect_raises_when_missing(base: Path):
    """Collect raises ValueError if any item is missing."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    keys = [cache.key("exists"), cache.key("missing")]
    cache.save(keys[0], NoteEntry(code="exists", note="ok"))

    with pytest.raises(ValueError, match="not found"):
        cache.collect(keys)


# ---------------------------------------------------------------------------
# Schema-version stamp
# ---------------------------------------------------------------------------


class NoteEntryV2(BaseModel):
    """Same name, extra required field — simulates a real schema migration."""

    code: str
    note: str
    confidence: float


def test_save_writes_schema_version_stamp(base: Path):
    """Persisted files include the schema-version wrapper so older readers can refuse them."""
    import json

    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    key = cache.key("stamp")
    cache.save(key, NoteEntry(code="stamp", note="x"))
    raw = json.loads((base / ".items" / "notes" / f"{key}.json").read_text())
    assert "_schema_version" in raw
    assert "_data" in raw
    assert raw["_data"]["code"] == "stamp"


def test_load_rejects_schema_version_mismatch(base: Path):
    """A cache written under schema V1 is unreadable under V2; partition treats it as a miss."""
    v1 = ItemCache[NoteEntry](base, "notes", NoteEntry)
    key = v1.key("migrate")
    v1.save(key, NoteEntry(code="migrate", note="v1 payload"))

    v2 = ItemCache[NoteEntryV2](base, "notes", NoteEntryV2)
    # load() must raise on mismatch so callers can decide what to do.
    with pytest.raises(ValueError, match="schema_version mismatch"):
        v2.load(key)

    # partition() catches the exception and reports the entry as stale.
    cached, uncached = v2.partition([key])
    assert cached == {}
    assert uncached == [0]


def test_load_falls_back_to_legacy_unstamped_files(base: Path):
    """Pre-stamp caches (top-level Pydantic JSON, no wrapper) still load."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    key = cache.key("legacy")
    # Write a raw Pydantic dump (the pre-feature format).
    path = base / ".items" / "notes" / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NoteEntry(code="legacy", note="old format").model_dump_json())
    loaded = cache.load(key)
    assert loaded.code == "legacy"
    assert loaded.note == "old format"


# ---------------------------------------------------------------------------
# In-flight WAL
# ---------------------------------------------------------------------------


def test_mark_and_clear_inflight(base: Path):
    """Mark writes the sentinel; clear removes it; orphaned markers are listable."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    key = cache.key("work")

    assert cache.inflight_keys() == set()
    cache.mark_inflight(key)
    assert key in cache.inflight_keys()

    cache.clear_inflight(key)
    assert cache.inflight_keys() == set()


def test_clear_inflight_idempotent(base: Path):
    """Clearing a non-existent inflight marker is a no-op, not an error."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    cache.clear_inflight(cache.key("never_marked"))  # must not raise


def test_save_clears_inflight(base: Path):
    """Save() implicitly clears any inflight sentinel for the same key."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    key = cache.key("done")
    cache.mark_inflight(key)
    cache.save(key, NoteEntry(code="done", note="finished"))
    assert cache.inflight_keys() == set()


def test_partition_reports_inflight_orphans_via_log(base: Path):
    """An inflight marker left behind by a crashed run is observable via inflight_keys()."""
    cache = ItemCache[NoteEntry](base, "notes", NoteEntry)
    key = cache.key("orphaned")
    cache.mark_inflight(key)
    # No save() — simulating a crashed worker.
    assert key in cache.inflight_keys()
    cached, uncached = cache.partition([key])
    # The entry is still uncached (no result file) — the marker is informational.
    assert cached == {}
    assert uncached == [0]


def test_pipeline_creates_item_cache(tmp_path: Path):
    """Pipeline.item_cache() creates a cache scoped to the pipeline's base."""
    from dataclasses import dataclass

    from adag.pipeline import Pipeline
    from adag.step import Step

    @dataclass(frozen=True)
    class DummyStep(Step[NoteEntry]):
        """Dummy for testing."""

        def run(self) -> NoteEntry:
            return NoteEntry(code="x", note="y")

    graph = DummyStep()()
    pipeline = Pipeline(graph, cache_dir=tmp_path / "data")
    cache = pipeline.item_cache("notes", NoteEntry)
    assert cache.cache_dir == pipeline.cache_dir
    assert cache.namespace == "notes"


def test_positional_signature_is_frozen(tmp_path: Path) -> None:
    """``ItemCache(cache_dir, namespace, schema)`` keeps its first-release positional order.

    Old code builds it positionally; a reorder of two ``str``/``Path`` parameters would not fail
    loudly, it would write to the wrong place.
    """
    import inspect

    params = list(inspect.signature(ItemCache).parameters.values())
    assert [p.name for p in params[:3]] == ["cache_dir", "namespace", "schema"]
    assert all(p.kind is p.KEYWORD_ONLY for p in params[3:])

    cache = ItemCache(tmp_path, "ns", NoteEntry)
    cache.save("k", NoteEntry(code="c", note="n"))
    assert tmp_path.joinpath(".items", "ns", "k.json").exists()
