"""Tests for BatchStep — per-item caching with resumability."""

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import BatchItem, BatchStep, FileSource, Pipeline, Step
from adag.deprecation import AdagDeprecationWarning


class ItemResult(BaseModel):
    value: str
    doubled: str


class BatchResult(BaseModel):
    items: list[ItemResult]
    total: int


class SourceData(BaseModel):
    names: list[str]


@dataclass(frozen=True)
class SourceStep(Step[SourceData]):
    """Reads names from a file."""

    data: FileSource

    def run(self, data: str) -> SourceData:
        return SourceData(names=data.strip().splitlines())


@dataclass(frozen=True)
class DoubleBatch(BatchStep[BatchResult, ItemResult]):
    """Doubles each name."""

    prefix: str = ""

    def items(self, source: SourceData, prefix: str) -> list[BatchItem]:
        return [BatchItem(key=f"{prefix}_{name}", data=name) for name in source.names]

    def process_item(self, item: BatchItem, source: SourceData, prefix: str) -> ItemResult:
        return ItemResult(value=item.data, doubled=item.data * 2)

    def assemble(self, results: list[ItemResult], source: SourceData, prefix: str) -> BatchResult:
        return BatchResult(items=results, total=len(results))


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    f = tmp_path / "names.txt"
    f.write_text("alice\nbob\ncharlie\n")
    return f


def test_batch_step_processes_all_items(tmp_path: Path, source_file: Path):
    """All items are processed and assembled."""
    batch = DoubleBatch()
    source = SourceStep(data=FileSource(str(source_file)))
    result_deferred = batch(source=source(data=FileSource(str(source_file))))
    pipeline = Pipeline(output=result_deferred, cache_dir=tmp_path / "cache")

    result = pipeline.execute()

    assert result.total == 3
    assert result.items[0].value == "alice"
    assert result.items[0].doubled == "alicealice"
    assert result.items[2].value == "charlie"


def test_batch_step_caches_items(tmp_path: Path, source_file: Path):
    """Second run skips processing — all items cached."""
    batch = DoubleBatch()
    source = SourceStep(data=FileSource(str(source_file)))
    result_deferred = batch(source=source(data=FileSource(str(source_file))))
    pipeline = Pipeline(output=result_deferred, cache_dir=tmp_path / "cache")

    first_result = pipeline.execute()
    second_result = pipeline.execute()

    assert first_result.total == second_result.total
    assert first_result.items == second_result.items


def test_batch_step_resumes_after_crash(tmp_path: Path, source_file: Path):
    """Simulate crash: pre-cache 2 of 3 items, then run — only 1 item reprocessed."""
    batch = DoubleBatch()

    from adag.item_cache import ItemCache

    cache = ItemCache(tmp_path / "cache", batch.item_cache_namespace(), ItemResult)
    cache.save("_alice", ItemResult(value="alice", doubled="alicealice"))
    cache.save("_bob", ItemResult(value="bob", doubled="bobbob"))

    source = SourceStep(data=FileSource(str(source_file)))
    result_deferred = batch(source=source(data=FileSource(str(source_file))))
    pipeline = Pipeline(output=result_deferred, cache_dir=tmp_path / "cache")

    result = pipeline.execute()

    assert result.total == 3
    assert result.items[2].value == "charlie"
    assert result.items[2].doubled == "charliecharlie"


def test_batch_step_different_prefix_different_cache(tmp_path: Path, source_file: Path):
    """Different prefix produces different cache keys."""
    batch_a = DoubleBatch(prefix="a")
    batch_b = DoubleBatch(prefix="b")

    assert batch_a.content_hash() != batch_b.content_hash()


def test_batch_step_empty_items(tmp_path: Path):
    """Zero items produces empty result without crashing."""
    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("")

    source = SourceStep(data=FileSource(str(empty_file)))
    batch = DoubleBatch()
    result_deferred = batch(source=source(data=FileSource(str(empty_file))))
    pipeline = Pipeline(output=result_deferred, cache_dir=tmp_path / "cache")

    result = pipeline.execute()
    assert result.total == 0
    assert result.items == []


def test_batch_step_namespace_from_class_name():
    """Item cache namespace defaults to snake_case class name."""
    batch = DoubleBatch()
    assert batch.item_cache_namespace() == "double_batch"


# ---------------------------------------------------------------------------
# Auto-key path: subclasses override items_data() and the framework derives
# the BatchItem.key from (step.content_hash, item_data).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoKeyBatch(BatchStep[BatchResult, ItemResult]):
    """Same logic as DoubleBatch, but uses items_data() so the framework auto-keys."""

    # `source` is the container of items; per-item keys derive from each
    # name extracted by items_data, not from the whole source list. This
    # is the typical pattern: declare which kwarg is the items container.
    items_source_keys = ("source",)

    prefix: str = ""

    def items_data(self, source: SourceData, prefix: str):  # type: ignore[override]
        return list(source.names)

    def process_item(self, item: BatchItem, source: SourceData, prefix: str) -> ItemResult:
        return ItemResult(value=item.data, doubled=item.data * 2)

    def assemble(self, results: list[ItemResult], source: SourceData, prefix: str) -> BatchResult:
        return BatchResult(items=results, total=len(results))


def test_auto_key_runs_end_to_end(tmp_path: Path, source_file: Path):
    """A BatchStep that only defines items_data() runs cleanly via the default items()."""
    batch = AutoKeyBatch()
    source = SourceStep(data=FileSource(str(source_file)))
    result_deferred = batch(source=source(data=FileSource(str(source_file))))
    pipeline = Pipeline(output=result_deferred, cache_dir=tmp_path / "cache")
    result = pipeline.execute()
    assert {item.value for item in result.items} == {"alice", "bob", "charlie"}


def test_auto_key_is_deterministic(source_file: Path):
    """Same scaffolding → same auto-generated keys across calls."""
    batch = AutoKeyBatch()
    src_data = SourceData(names=["alice", "bob"])
    items_a = batch.items(source=src_data, prefix="")
    items_b = batch.items(source=src_data, prefix="")
    assert [i.key for i in items_a] == [i.key for i in items_b]


def test_auto_key_changes_with_step_params(source_file: Path):
    """Changing the step's content (e.g. `prefix`) flips every item's key.

    This is the regression-protection test for the cache-poisoning bug class:
    if the step's params change, the items' cache identities change too.
    """
    src_data = SourceData(names=["alice", "bob"])
    a = AutoKeyBatch(prefix="x")
    b = AutoKeyBatch(prefix="y")
    keys_a = [i.key for i in a.items(source=src_data, prefix="x")]
    keys_b = [i.key for i in b.items(source=src_data, prefix="y")]
    # Same item data, different step → entirely different keys.
    assert set(keys_a).isdisjoint(set(keys_b))


def test_auto_key_changes_with_item_data():
    """Different item payloads produce different auto-keys."""
    batch = AutoKeyBatch()
    src1 = SourceData(names=["a", "b"])
    src2 = SourceData(names=["a", "c"])
    k1 = [i.key for i in batch.items(source=src1, prefix="")]
    k2 = [i.key for i in batch.items(source=src2, prefix="")]
    # First element same, second differs.
    assert k1[0] == k2[0]
    assert k1[1] != k2[1]


@dataclass(frozen=True)
class NoOverrideBatch(BatchStep[BatchResult, ItemResult]):
    """A subclass that defines neither items() nor items_data() — must raise."""

    def process_item(self, item: BatchItem, source: SourceData) -> ItemResult:
        return ItemResult(value="x", doubled="xx")

    def assemble(self, results: list[ItemResult], source: SourceData) -> BatchResult:
        return BatchResult(items=results, total=len(results))


def test_no_override_raises_helpful_error():
    """If a subclass overrides neither items() nor items_data(), we raise a clear error."""
    batch = NoOverrideBatch()
    with pytest.raises(NotImplementedError, match="items_data|items"):
        batch.items(source=SourceData(names=["a"]))


# ---------------------------------------------------------------------------
# In-flight WAL is wired into the default process_batch
# ---------------------------------------------------------------------------


def test_process_batch_clears_inflight_on_success(tmp_path: Path, source_file: Path):
    """After a clean run no inflight markers should remain."""
    from adag.item_cache import ItemCache

    batch = AutoKeyBatch()
    source = SourceStep(data=FileSource(str(source_file)))
    result_deferred = batch(source=source(data=FileSource(str(source_file))))
    pipeline = Pipeline(output=result_deferred, cache_dir=tmp_path / "cache")
    pipeline.execute()
    cache = ItemCache[ItemResult](tmp_path / "cache", batch.item_cache_namespace(), ItemResult)
    assert cache.inflight_keys() == set()


def test_process_batch_leaves_inflight_marker_on_exception(tmp_path: Path, source_file: Path):
    """If process_item raises, the inflight marker survives so the next resume sees it."""
    from adag.item_cache import ItemCache

    @dataclass(frozen=True)
    class CrashBatch(BatchStep[BatchResult, ItemResult]):
        def items_data(self, source: SourceData):
            return list(source.names)

        def process_item(self, item: BatchItem, source: SourceData) -> ItemResult:
            raise RuntimeError("boom on " + item.data)

        def assemble(self, results: list[ItemResult], source: SourceData) -> BatchResult:
            return BatchResult(items=results, total=len(results))

    source = SourceStep(data=FileSource(str(source_file)))
    batch = CrashBatch()
    pipeline = Pipeline(
        output=batch(source=source(data=FileSource(str(source_file)))),
        cache_dir=tmp_path / "cache",
    )
    with pytest.raises(RuntimeError, match="boom"):
        pipeline.execute()
    cache = ItemCache[ItemResult](tmp_path / "cache", batch.item_cache_namespace(), ItemResult)
    # At least one item was in-flight when the crash happened.
    assert len(cache.inflight_keys()) >= 1


def test_step_field_wired_again_as_input_warns_and_runs(tmp_path: Path, source_file: Path):
    """The first-release pattern (upstream step as a field and as an input) warns and still runs."""

    @dataclass(frozen=True)
    class TwiceBatch(BatchStep[BatchResult, ItemResult]):
        source: Step[SourceData]

        def items_data(self, source: SourceData):
            return list(source.names)

        def process_item(self, item: BatchItem, source: SourceData) -> ItemResult:
            return ItemResult(value=item.data, doubled=item.data * 2)

        def assemble(self, results: list[ItemResult], source: SourceData) -> BatchResult:
            return BatchResult(items=results, total=len(results))

    source = SourceStep(data=FileSource(str(source_file)))
    with pytest.warns(AdagDeprecationWarning, match="both a Step field and an input"):
        wired = TwiceBatch(source=source)(source=source(data=FileSource(str(source_file))))
    result = Pipeline(wired, cache_dir=tmp_path / "cache").execute()
    assert result.total == 3
