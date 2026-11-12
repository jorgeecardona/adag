"""Steps whose result is not one JSON document: ``save_result`` / ``load_result``."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel, Field
from structlog.testing import capture_logs

from adag import BatchItem, BatchStep, InputParam, Pipeline, Step
from adag.cache_store import MemoryCacheStore

CALLS: dict[str, int] = {}


def _count(name: str) -> None:
    CALLS[name] = CALLS.get(name, 0) + 1


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    CALLS.clear()


@dataclass(frozen=True)
class Records(Step[list[dict[str, int]]]):
    """Writes its result as JSONL, one record per line."""

    def run(self, n: int) -> list[dict[str, int]]:
        _count("records")
        return [{"i": i} for i in range(n)]

    def save_result(self, result: list[dict[str, int]], path: Path) -> None:
        path.write_text("".join(json.dumps(r) + "\n" for r in result))

    def load_result(self, path: Path) -> list[dict[str, int]]:
        return [json.loads(line) for line in path.read_text().splitlines()]


class Summary(BaseModel):
    total: int


@dataclass(frozen=True)
class WithSidecar(Step[Summary]):
    """Bulk lines plus a sidecar; load reads only the sidecar."""

    def run(self, n: int) -> Summary:
        _count("sidecar")
        return Summary(total=n)

    def save_result(self, result: Summary, path: Path) -> None:
        path.mkdir()
        path.joinpath("bulk.txt").write_text("x\n" * result.total)
        path.joinpath("result.meta.json").write_text(result.model_dump_json())

    def load_result(self, path: Path) -> Summary:
        return Summary.model_validate_json(path.joinpath("result.meta.json").read_text())


@dataclass(frozen=True)
class Partitions(Step[Path]):
    """A directory with one file per partition; the result is the directory itself."""

    def run(self, n: int) -> Path:
        _count("partitions")
        return Path(f"/nonexistent/{n}")  # replaced by the cached path on save

    def save_result(self, result: Path, path: Path) -> None:
        path.mkdir()
        for i in range(3):
            path.joinpath(f"part-{i}.txt").write_text(str(i))

    def load_result(self, path: Path) -> Path:
        if not path.joinpath("part-2.txt").exists():
            raise ValueError("incomplete")
        return path


def test_jsonl_result_is_cached_and_read_back(tmp_path: Path) -> None:
    pipeline = Pipeline(Records()(n=InputParam[int]("n")), cache_dir=tmp_path)
    assert pipeline.execute(n=3) == [{"i": 0}, {"i": 1}, {"i": 2}]
    assert pipeline.execute(n=3) == [{"i": 0}, {"i": 1}, {"i": 2}]
    assert CALLS["records"] == 1


def test_different_inputs_get_different_paths(tmp_path: Path) -> None:
    """The path is keyed on the node (step and inputs), so there is no stale read."""
    pipeline = Pipeline(Records()(n=InputParam[int]("n")), cache_dir=tmp_path)
    assert len(pipeline.execute(n=2)) == 2
    assert len(pipeline.execute(n=5)) == 5
    assert CALLS["records"] == 2


def test_file_plus_sidecar_result(tmp_path: Path) -> None:
    pipeline = Pipeline(WithSidecar()(n=InputParam[int]("n")), cache_dir=tmp_path)
    assert pipeline.execute(n=4).total == 4
    assert pipeline.execute(n=4).total == 4
    assert CALLS["sidecar"] == 1


def test_directory_result_gets_its_final_path(tmp_path: Path) -> None:
    pipeline = Pipeline(Partitions()(n=InputParam[int]("n")), cache_dir=tmp_path)
    first = pipeline.execute(n=1)
    assert first.parent == tmp_path
    assert sorted(p.name for p in first.iterdir()) == ["part-0.txt", "part-1.txt", "part-2.txt"]
    assert pipeline.execute(n=1) == first
    assert CALLS["partitions"] == 1
    assert not list(tmp_path.glob("*.tmp-*"))


def test_invalid_entry_is_a_miss_and_recomputes(tmp_path: Path) -> None:
    pipeline = Pipeline(Partitions()(n=InputParam[int]("n")), cache_dir=tmp_path)
    first = pipeline.execute(n=1)
    first.joinpath("part-2.txt").unlink()
    again = pipeline.execute(n=1)
    assert again.joinpath("part-2.txt").exists()
    assert CALLS["partitions"] == 2


def test_is_cached_agrees_with_the_resolver_on_an_invalid_entry(tmp_path: Path) -> None:
    """An entry that load_result rejects is not cached for is_cached either (#5)."""
    node = Partitions()(n=InputParam[int]("n"))
    first = Pipeline(node, cache_dir=tmp_path).execute(n=1)
    assert node.is_cached(tmp_path, params={"n": 1})
    first.joinpath("part-2.txt").unlink()
    assert not node.is_cached(tmp_path, params={"n": 1})


def test_store_only_run_raises_before_the_body(tmp_path: Path) -> None:
    pipeline = Pipeline(Records()(n=InputParam[int]("n")), store=MemoryCacheStore())
    with pytest.raises(ValueError, match="cache_dir"):
        pipeline.execute(n=1)
    assert "records" not in CALLS


def test_overriding_only_one_hook_is_rejected() -> None:
    with pytest.raises(TypeError, match="save_result/load_result"):

        @dataclass(frozen=True)
        class OnlySave(Step[Summary]):  # pyright: ignore[reportUnusedClass]
            def run(self) -> Summary:
                return Summary(total=0)

            def save_result(self, result: Summary, path: Path) -> None:
                path.write_text("x")


class Item(BaseModel):
    value: int


class Squares(BaseModel):
    values: list[int]


@dataclass(frozen=True)
class SquaresToJsonl(BatchStep[Squares, Item]):
    """Per-item cache for the items, one JSONL file for the assembled result."""

    def items_data(self, n: int) -> list[int]:
        return list(range(n))

    def process_item(self, item: BatchItem, n: int) -> Item:
        _count("item")
        return Item(value=item.data * item.data)

    def assemble(self, results: list[Item], n: int) -> Squares:
        _count("assemble")
        return Squares(values=[r.value for r in results])

    def save_result(self, result: Squares, path: Path) -> None:
        path.write_text("".join(f"{v}\n" for v in result.values))

    def load_result(self, path: Path) -> Squares:
        return Squares(values=[int(line) for line in path.read_text().splitlines()])


def test_batch_step_caches_a_path_result_next_to_its_items(tmp_path: Path) -> None:
    pipeline = Pipeline(SquaresToJsonl()(n=InputParam[int]("n")), cache_dir=tmp_path)
    assert pipeline.execute(n=4).values == [0, 1, 4, 9]
    assert pipeline.execute(n=4).values == [0, 1, 4, 9]
    assert CALLS == {"item": 4, "assemble": 1}
    assert list(tmp_path.joinpath(".items").rglob("*.json"))


class Bulk(BaseModel):
    n_records: int
    records: list[int] = Field(default_factory=list[int], exclude=True)


@dataclass(frozen=True)
class LossyBulk(Step[Bulk]):
    """Writes only the summary, so the excluded field does not come back."""

    def run(self, n: int) -> Bulk:
        return Bulk(n_records=n, records=list(range(n)))

    def save_result(self, result: Bulk, path: Path) -> None:
        path.write_text(result.model_dump_json())

    def load_result(self, path: Path) -> Bulk:
        return Bulk.model_validate_json(path.read_text())


def test_lossy_round_trip_warns_on_first_save(tmp_path: Path) -> None:
    """Downstream sees load_result's value on the first run; a lossy one warns once (#7)."""
    with capture_logs() as logs:
        result = Pipeline(LossyBulk()(n=InputParam[int]("n")), cache_dir=tmp_path).execute(n=3)
    assert result.records == []
    assert [e["event"] for e in logs].count("step.load_result_differs") == 1


def test_faithful_round_trip_does_not_warn(tmp_path: Path) -> None:
    with capture_logs() as logs:
        Pipeline(Records()(n=InputParam[int]("n")), cache_dir=tmp_path).execute(n=2)
    assert "step.load_result_differs" not in [e["event"] for e in logs]
