"""Tests for Step, StepResult, and Pipeline — the core DAG engine."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Generic, TypeVar

import pytest
from pydantic import BaseModel, ConfigDict

from adag.config import Config
from adag.deferred import InputParam
from adag.hashing import UnhashableValueError
from adag.io import FileSource
from adag.pipeline import Pipeline
from adag.step import Step


class CountResult(BaseModel):
    """Simple result for testing."""

    count: int


class ConcatResult(BaseModel):
    """Result that concatenates strings."""

    text: str


@dataclass(frozen=True)
class AddStep(Step[CountResult]):
    """Adds two numbers."""

    a: int
    b: int

    def run(self, a: int, b: int) -> CountResult:
        return CountResult(count=a + b)


@dataclass(frozen=True)
class DoubleStep(Step[CountResult]):
    """Doubles an upstream result."""

    def run(self, source: CountResult) -> CountResult:
        return CountResult(count=source.count * 2)


@dataclass(frozen=True)
class TripleChainStep(Step[CountResult]):
    """Triples an upstream result."""

    def run(self, doubled: CountResult) -> CountResult:
        return CountResult(count=doubled.count * 3)


@dataclass(frozen=True)
class FileReaderStep(Step[ConcatResult]):
    """Reads a file and appends a suffix."""

    suffix: str = "!"

    def run(self, data: str, suffix: str) -> ConcatResult:
        return ConcatResult(text=data.strip() + suffix)


@dataclass(frozen=True)
class ConfigStep(Step[CountResult]):
    """Has a Config field that should NOT affect the hash."""

    value: int
    batch_size: Config[int] = Config(32)

    def run(self, value: int, batch_size: int) -> CountResult:
        return CountResult(count=value * batch_size)


def test_hash_deterministic():
    """Same inputs produce the same hash."""
    assert AddStep(a=1, b=2).content_hash() == AddStep(a=1, b=2).content_hash()


def test_hash_changes_with_input():
    """Different inputs produce different hashes."""
    assert AddStep(a=1, b=2).content_hash() != AddStep(a=1, b=3).content_hash()


def test_hash_config_excluded():
    """Config fields do not affect the hash."""
    step_a = ConfigStep(value=10, batch_size=Config(32))
    step_b = ConfigStep(value=10, batch_size=Config(64))
    assert step_a.content_hash() == step_b.content_hash()


def test_hash_config_value_still_matters():
    """Non-Config fields still affect the hash."""
    step_a = ConfigStep(value=10)
    step_b = ConfigStep(value=20)
    assert step_a.content_hash() != step_b.content_hash()


def test_hash_excludes_node_label():
    """`node_label` is a display-only label for the graph renderer, not compute identity:
    two steps that differ only by label share a content hash; other fields still matter.
    """

    @dataclass(frozen=True)
    class LabelledStep(Step[CountResult]):
        node_label: str = ""
        n: int = 0

        def run(self) -> CountResult:
            return CountResult(count=self.n)

    assert (
        LabelledStep(node_label="parent", n=1).content_hash()
        == LabelledStep(node_label="child", n=1).content_hash()
    )
    assert (
        LabelledStep(node_label="parent", n=1).content_hash()
        != LabelledStep(node_label="parent", n=2).content_hash()
    )


def test_hash_with_file_source(tmp_path: Path):
    """FileSource fields hash by file content."""
    file_path = tmp_path / "input.txt"
    file_path.write_text("hello")

    @dataclass(frozen=True)
    class FileStep(Step[ConcatResult]):
        """Step with a FileSource field."""

        data: FileSource

        def run(self, data: str) -> ConcatResult:
            return ConcatResult(text=data)

    step = FileStep(data=FileSource(str(file_path)))
    assert step.content_hash() is not None


def test_hash_none_when_file_missing():
    """Missing FileSource blocks the step (hash returns None)."""

    @dataclass(frozen=True)
    class FileStep(Step[ConcatResult]):
        """Step with a missing FileSource."""

        data: FileSource

        def run(self, data: str) -> ConcatResult:
            return ConcatResult(text=data)

    step = FileStep(data=FileSource("/nonexistent/file.txt"))
    assert step.content_hash() is None


def test_execute_produces_result(tmp_path: Path):
    """Execute runs the step and returns the result."""
    add = AddStep(a=3, b=4)
    graph = add(a=3, b=4)
    pipeline = Pipeline(graph, cache_dir=tmp_path)
    result = pipeline.execute()
    assert result.count == 7


def test_execute_caches_result(tmp_path: Path):
    """After execute, the step result is cached on disk."""
    add = AddStep(a=3, b=4)
    graph = add(a=3, b=4)
    pipeline = Pipeline(graph, cache_dir=tmp_path)
    pipeline.execute()
    assert graph.is_cached(tmp_path)


def test_execute_without_cache_dir_writes_no_files(tmp_path: Path):
    """When cache_dir is None, execute() must not write any cache file.

    A cache-less run must leave the filesystem untouched.
    """
    add = AddStep(a=3, b=4)
    graph = add(a=3, b=4)
    pipeline = Pipeline(graph)
    pipeline.execute()
    assert list(tmp_path.iterdir()) == []
    assert not graph.is_cached(tmp_path)


@pytest.mark.parametrize("bad_content", ["", "   \n", "{not json"])
def test_empty_or_corrupt_cache_file_is_a_miss(tmp_path: Path, bad_content: str):
    """An empty or corrupt cache file is recomputed, never returned or crashed on."""
    graph = AddStep(a=3, b=4)(a=3, b=4)
    pipeline = Pipeline(graph, cache_dir=tmp_path)
    pipeline.execute()
    [output] = [f for f in tmp_path.rglob("*") if f.is_file() and f.suffix != ".done"]
    output.write_text(bad_content)

    assert not graph.is_cached(tmp_path)
    assert pipeline.execute().count == 7
    assert graph.is_cached(tmp_path)


def test_execute_uses_cache(tmp_path: Path):
    """Second execute returns cached result without re-running."""
    add = AddStep(a=3, b=4)
    graph = add(a=3, b=4)
    pipeline = Pipeline(graph, cache_dir=tmp_path)
    result1 = pipeline.execute()
    result2 = pipeline.execute()
    assert result1.count == result2.count


def test_execute_upstream_auto_runs(tmp_path: Path):
    """Upstream steps are automatically executed when not cached."""
    add = AddStep(a=2, b=3)
    double = DoubleStep()
    graph = double(source=add(a=2, b=3))
    pipeline = Pipeline(graph, cache_dir=tmp_path)
    result = pipeline.execute()
    assert result.count == 10


def test_execute_deep_chain(tmp_path: Path):
    """Three-level chain executes correctly."""
    add = AddStep(a=1, b=2)
    double = DoubleStep()
    triple = TripleChainStep()
    graph = triple(doubled=double(source=add(a=1, b=2)))
    pipeline = Pipeline(graph, cache_dir=tmp_path)
    result = pipeline.execute()
    assert result.count == 18  # (1+2)*2*3


def test_execute_shared_upstream(tmp_path: Path):
    """Two steps sharing the same upstream only execute it once."""
    add = AddStep(a=5, b=5)

    @dataclass(frozen=True)
    class BranchA(Step[CountResult]):
        """Branch A."""

        def run(self, source: CountResult) -> CountResult:
            return CountResult(count=source.count + 1)

    @dataclass(frozen=True)
    class BranchB(Step[CountResult]):
        """Branch B."""

        def run(self, source: CountResult) -> CountResult:
            return CountResult(count=source.count + 2)

    @dataclass(frozen=True)
    class Merge(Step[CountResult]):
        """Merges two branches."""

        def run(self, branch_a: CountResult, branch_b: CountResult) -> CountResult:
            return CountResult(count=branch_a.count + branch_b.count)

    add_result = add(a=5, b=5)
    a_result = BranchA()(source=add_result)
    b_result = BranchB()(source=add_result)
    graph = Merge()(branch_a=a_result, branch_b=b_result)
    pipeline = Pipeline(graph, cache_dir=tmp_path)
    result = pipeline.execute()
    assert result.count == 23  # (10+1) + (10+2)


def test_shared_upstream_resolves_to_the_same_instance(tmp_path: Path):
    """A shared upstream hands every consumer the SAME object by identity.

    Per-execute memoization (ResolveContext._resolved) means a StepResult
    consumed by two downstream sources resolves once and both consumers get
    the identical instance — not an independent re-execution that yields two
    distinct objects. Non-serializable results (e.g. a torch model handle)
    depend on this shared-instance contract.
    """

    @dataclass(frozen=True)
    class Producer(Step[CountResult]):
        """Returns a fresh result object each time it runs."""

        def run(self) -> CountResult:
            return CountResult(count=1)

    @dataclass(frozen=True)
    class IdentityMerge(Step[CountResult]):
        """Reports 1 only when its two upstream inputs are the same object."""

        def run(self, left: CountResult, right: CountResult) -> CountResult:
            return CountResult(count=1 if left is right else 0)

    producer = Producer()()
    graph = IdentityMerge()(left=producer, right=producer)
    result = Pipeline(graph, cache_dir=tmp_path).execute()
    assert result.count == 1


def test_execute_with_file_source(tmp_path: Path):
    """FileSource inputs are loaded as text."""
    file_path = tmp_path / "input.txt"
    file_path.write_text("hello world")
    source = FileSource(str(file_path))
    step = FileReaderStep()
    graph = step(data=source, suffix="!")
    pipeline = Pipeline(graph)
    result = pipeline.execute()
    assert result.text == "hello world!"


def test_execute_with_parameters(tmp_path: Path):
    """InputParam values are resolved at execution time."""
    text = InputParam[str]("text")

    @dataclass(frozen=True)
    class EchoStep(Step[ConcatResult]):
        """Echoes text."""

        def run(self, text: str) -> ConcatResult:
            return ConcatResult(text=text)

    graph = EchoStep()(text=text)
    pipeline = Pipeline(graph)
    result = pipeline.execute(text="hello")
    assert result.text == "hello"


def test_execute_missing_parameter_raises():
    """Missing required parameters raise ValueError."""
    text = InputParam[str]("text")

    @dataclass(frozen=True)
    class EchoStep(Step[ConcatResult]):
        """Echoes text."""

        def run(self, text: str) -> ConcatResult:
            return ConcatResult(text=text)

    graph = EchoStep()(text=text)
    pipeline = Pipeline(graph)
    with pytest.raises(ValueError, match="Missing parameters"):
        pipeline.execute()


def test_execute_default_parameter():
    """Default parameters are used when not provided."""
    threshold = InputParam.with_default("threshold", 0.5)

    @dataclass(frozen=True)
    class ThresholdStep(Step[CountResult]):
        """Uses a threshold."""

        def run(self, threshold: float) -> CountResult:
            return CountResult(count=int(threshold * 100))

    graph = ThresholdStep()(threshold=threshold)
    pipeline = Pipeline(graph)
    result = pipeline.execute()
    assert result.count == 50


def test_cache_key_pinning():
    """Pinned cache_key overrides computed hash."""

    @dataclass(frozen=True)
    class PinnedStep(Step[CountResult]):
        """Step with pinned cache key."""

        cache_key = "my_pinned_v1"
        value: int

        def run(self, value: int) -> CountResult:
            return CountResult(count=value)

    assert PinnedStep(value=1).content_hash() == "my_pinned_v1"
    assert PinnedStep(value=999).content_hash() == "my_pinned_v1"


def test_non_cached_step_always_runs(tmp_path: Path):
    """A Step without result_type always runs, no caching."""
    call_count = 0

    class UploadResult(BaseModel):
        """Result for non-cached step."""

        uploaded: bool

    @dataclass(frozen=True)
    class UploadStep(Step[UploadResult]):
        """Always runs — explicitly opt out of caching."""

        result_type = None
        name: str

        def run(self, name: str) -> UploadResult:
            nonlocal call_count
            call_count += 1
            return UploadResult(uploaded=True)

    step = UploadStep(name="test")
    graph = step(name="test")
    pipeline = Pipeline(graph, cache_dir=tmp_path)
    pipeline.execute()
    pipeline.execute()
    assert call_count == 2


def test_list_input_hashed():
    """List fields are included in the hash."""

    @dataclass(frozen=True)
    class ListStep(Step[CountResult]):
        """Step with list input."""

        labels: list[str]

        def run(self, labels: list[str]) -> CountResult:
            return CountResult(count=len(labels))

    step_a = ListStep(labels=["a", "b"])
    step_b = ListStep(labels=["a", "b", "c"])
    assert step_a.content_hash() != step_b.content_hash()


def test_tuple_field_hashed():
    """Tuple fields hash like lists — useful for immutable config (categories, axes)."""

    @dataclass(frozen=True)
    class TupleStep(Step[CountResult]):
        """Step with a tuple config field."""

        categories: tuple[str, ...]

        def run(self, categories: tuple[str, ...]) -> CountResult:
            return CountResult(count=len(categories))

    step_a = TupleStep(categories=("a", "b"))
    step_b = TupleStep(categories=("a", "b", "c"))
    assert step_a.content_hash() is not None
    assert step_a.content_hash() != step_b.content_hash()
    assert step_a.content_hash() == TupleStep(categories=("a", "b")).content_hash()


def test_tuple_and_list_with_same_contents_hash_equal():
    """tuple(...) and list(...) with identical contents collide by design — the framework
    treats them as the same sequence so authors can pick whichever fits the dataclass best.
    """

    @dataclass(frozen=True)
    class SeqStep(Step[CountResult]):
        """Sequence config."""

        items: object  # Any so dataclass accepts list or tuple at instantiation.

        def run(self) -> CountResult:
            return CountResult(count=len(self.items))  # type: ignore[arg-type]

    step_tuple = SeqStep(items=("x", "y", "z"))
    step_list = SeqStep(items=["x", "y", "z"])
    assert step_tuple.content_hash() == step_list.content_hash()


def test_nested_tuple_in_list_field_hashed():
    """Items inside a list/tuple field can themselves be tuples (e.g. ordered cap mappings)."""

    @dataclass(frozen=True)
    class CapsStep(Step[CountResult]):
        """Step with a tuple-of-tuples config."""

        caps: tuple[tuple[str, int], ...]

        def run(self) -> CountResult:
            return CountResult(count=sum(v for _, v in self.caps))

    step_a = CapsStep(caps=(("foo", 1), ("bar", 2)))
    step_b = CapsStep(caps=(("foo", 1), ("bar", 3)))
    assert step_a.content_hash() is not None
    assert step_a.content_hash() != step_b.content_hash()


def test_pipeline_steps_in_dependency_order(tmp_path: Path):
    """Pipeline collects steps in dependency order (upstream first)."""
    add = AddStep(a=1, b=2)
    double = DoubleStep()
    triple = TripleChainStep()
    graph = triple(doubled=double(source=add(a=1, b=2)))
    pipeline = Pipeline(graph, cache_dir=tmp_path)
    step_names = [type(s).__name__ for s in pipeline.steps]
    assert step_names == ["AddStep", "DoubleStep", "TripleChainStep"]


def test_pipeline_discovers_parameters():
    """Pipeline discovers all InputParams in the graph."""
    text = InputParam[str]("text")
    threshold = InputParam[float].with_default("threshold", 0.5)

    @dataclass(frozen=True)
    class TwoParamStep(Step[CountResult]):
        """Uses two parameters."""

        def run(self, text: str, threshold: float) -> CountResult:
            return CountResult(count=len(text))

    graph = TwoParamStep()(text=text, threshold=threshold)
    pipeline = Pipeline(graph)
    param_names = [p.name for p in pipeline._inputs]
    assert "text" in param_names
    assert "threshold" in param_names


def test_pipeline_repr():
    """Pipeline repr shows steps and inputs."""

    @dataclass(frozen=True)
    class SimpleStep(Step[CountResult]):
        """Simple."""

        def run(self, value: int) -> CountResult:
            return CountResult(count=value)

    graph = SimpleStep()(value=42)
    pipeline = Pipeline(graph)
    assert "SimpleStep" in repr(pipeline)


def test_result_type_auto_detected():
    """Step[SomeBaseModel] auto-detects result_type for caching."""
    assert AddStep.result_type is CountResult
    assert DoubleStep.result_type is CountResult


def test_no_result_type_for_non_basemodel():
    """Step with non-BaseModel output has no result_type."""

    @dataclass(frozen=True)
    class RawStep(Step[dict[str, int]]):
        """Returns a plain dict."""

        def run(self) -> dict[str, int]:
            return {"a": 1}

    assert RawStep.result_type is None


def test_validate_accepts_subclass_at_wiring_time():
    """A Step declaring a parent type as input accepts a Step producing a subclass.

    Liskov substitution at the wiring layer — the alternative (exact-class equality)
    forces every "accepts any of these subclasses" sink to be typed `Any` with a
    runtime isinstance check, which is exactly what WriteJSONL used to do.
    """

    class _Parent(BaseModel):
        value: int

    class _Child(_Parent):
        extra: str = "x"

    @dataclass(frozen=True)
    class _ProducesChild(Step[_Child]):
        def run(self) -> _Child:
            return _Child(value=1)

    @dataclass(frozen=True)
    class _ConsumesParent(Step[CountResult]):
        def run(self, item: _Parent) -> CountResult:
            return CountResult(count=item.value)

    # This wiring used to raise TypeError("Type mismatch: ... produces _Child,
    # but ... expects _Parent for 'item'"); with issubclass it must succeed.
    result = _ConsumesParent()(item=_ProducesChild()())
    assert Pipeline(result).execute().count == 1


def test_pipeline_save_captures_basemodel_spec_field(tmp_path: Path):
    """Pipeline.save's step.json spec now captures BaseModel-typed fields.

    Previously the inline isinstance(...) filter at pipeline.py:253 only
    accepted scalars/lists/tuples and silently dropped BaseModel — so
    every SerializableStep's `spec` field came out empty. The bug was
    masked because SerializableStep.save_artifacts overwrites step.json
    with the correct spec, but a non-SerializableStep with a BaseModel
    field would lose it entirely.
    """
    import json as _json
    from dataclasses import field as dataclass_field

    class Spec(BaseModel):
        model_name: str
        max_length: int = 128

    @dataclass(frozen=True)
    class _SpecStep(Step[CountResult]):
        kind = "tests/spec_step/v1"
        spec: Spec = dataclass_field(
            default_factory=lambda: Spec(model_name="test", max_length=256)
        )

        def run(self) -> CountResult:
            return CountResult(count=self.spec.max_length)

    pipeline = Pipeline(output=_SpecStep()())
    pipeline.save(tmp_path)

    step_json = _json.loads((tmp_path / "00__specstep" / "step.json").read_text())
    assert step_json["kind"] == "tests/spec_step/v1"
    assert step_json["spec"] == {"spec": {"model_name": "test", "max_length": 256}}


def test_pipeline_save_captures_path_uuid_enum_fields(tmp_path: Path):
    """Path / UUID / Enum fields round-trip to JSON via the typed serializer
    instead of the silent-drop + json.dump(default=str) fallback the old code used.
    """
    import json as _json
    from enum import StrEnum
    from uuid import UUID

    class Color(StrEnum):
        RED = "red"
        BLUE = "blue"

    from dataclasses import field as dataclass_field

    @dataclass(frozen=True)
    class _ExoticStep(Step[CountResult]):
        kind = "tests/exotic_step/v1"
        artifact_path: Path = dataclass_field(default_factory=lambda: Path("/tmp/artifact"))
        artifact_id: UUID = dataclass_field(
            default_factory=lambda: UUID("12345678-1234-5678-1234-567812345678")
        )
        color: Color = Color.RED

        def run(self) -> CountResult:
            return CountResult(count=1)

    pipeline = Pipeline(output=_ExoticStep()())
    pipeline.save(tmp_path)

    step_json = _json.loads((tmp_path / "00__exoticstep" / "step.json").read_text())
    assert step_json["spec"] == {
        "artifact_path": "/tmp/artifact",
        "artifact_id": "12345678-1234-5678-1234-567812345678",
        "color": "red",
    }


def test_validate_still_rejects_unrelated_types():
    """Subclass support shouldn't open the door to unrelated types — e.g. a Step
    producing CountResult must still be rejected where ConcatResult is declared.
    """

    @dataclass(frozen=True)
    class _ProducesCount(Step[CountResult]):
        def run(self) -> CountResult:
            return CountResult(count=1)

    @dataclass(frozen=True)
    class _ConsumesConcat(Step[CountResult]):
        def run(self, item: ConcatResult) -> CountResult:
            return CountResult(count=len(item.text))

    with pytest.raises(TypeError, match="Type mismatch"):
        _ConsumesConcat()(item=_ProducesCount()())


_T = TypeVar("_T", bound=BaseModel)


class _Pool(BaseModel, Generic[_T]):
    """A generic pool whose item type is only known per instance (like the synthetic engine's Dataset[T])."""

    model_config = ConfigDict(frozen=True)
    items: tuple[_T, ...]


@dataclass(frozen=True)
class _GenericPoolStep(Step[_Pool[_T]], Generic[_T]):
    """A generic-result step: __init_subclass__ cannot infer a parametrized result_type, so it overrides the hooks."""

    item_type: type[_T]

    def result_model(self) -> type[BaseModel]:
        return _Pool[self.item_type]

    def output_type(self) -> type[Any]:
        return _Pool[self.item_type]

    def run(self, **_: Any) -> _Pool[_T]:
        return _Pool(items=(self.item_type.model_validate({"count": 7}),))


def test_generic_step_caches_via_result_model_override(tmp_path: Path) -> None:
    """A generic Step[_Pool[_T]] caches and round-trips as the parametrized item type through the result_model hook."""
    step = _GenericPoolStep(item_type=CountResult)
    assert (
        step.result_model() is _Pool[CountResult]
    )  # the override, not the unparametrized inferred base
    graph = step()
    first = Pipeline(graph, cache_dir=tmp_path).execute()
    assert first.items[0].count == 7
    assert graph.is_cached(tmp_path)  # the override enabled an on-disk cache for a generic result

    cached = Pipeline(_GenericPoolStep(item_type=CountResult)(), cache_dir=tmp_path).execute()
    assert isinstance(
        cached.items[0], CountResult
    )  # loads from that cache as the concrete item type, not a dict
    assert cached.items[0].count == 7


def test_generic_step_type_field_distinguishes_cache_identity() -> None:
    """A type-valued identity field is hashed, so two parametrizations get distinct cache keys (no collision)."""
    pool_of_count = _GenericPoolStep(item_type=CountResult)()
    pool_of_concat = _GenericPoolStep(item_type=ConcatResult)()
    assert pool_of_count.content_hash() != pool_of_concat.content_hash()


class _Color(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass(frozen=True)
class _RichStep(Step[CountResult]):
    """A step with field types the old hash silently skipped."""

    path: Path = Path("a.txt")
    color: _Color = _Color.RED
    table: dict[str, int] = field(default_factory=dict[str, int])
    pair: tuple[int, int] = (1, 23)

    def run(self) -> CountResult:
        return CountResult(count=0)


@pytest.mark.parametrize(
    "changed",
    [
        {"path": Path("b.txt")},
        {"color": _Color.BLUE},
        {"table": {"x": 1}},
        {"pair": (12, 3)},
    ],
)
def test_every_field_change_changes_the_key(changed: dict[str, Any]):
    """Path, Enum, dict and tuple fields all reach the cache key (no silent skip, no collision)."""
    assert _RichStep(**changed).content_hash() != _RichStep().content_hash()


def test_unhashable_field_raises_with_config_hint():
    """A field the key cannot encode raises and names Config, instead of being skipped."""

    @dataclass(frozen=True)
    class HoldsClient(Step[CountResult]):
        client: object = field(default_factory=object)

        def run(self) -> CountResult:
            return CountResult(count=0)

    with pytest.raises(UnhashableValueError, match=r"client.*Config"):
        HoldsClient().content_hash()


def test_input_param_values_without_stable_encoding_raise(tmp_path: Path):
    """An InputParam value with no stable encoding raises (it used to hash by repr and never hit)."""
    graph = AddStep(a=0, b=0)(a=InputParam[Any]("a"), b=1)
    with pytest.raises(UnhashableValueError, match="InputParam 'a'"):
        Pipeline(graph, cache_dir=tmp_path).execute(a=object())


def test_input_param_values_hash_by_value_not_repr(tmp_path: Path):
    """Equal param values give equal keys; different values give different keys."""
    graph = AddStep(a=0, b=0)(a=InputParam[int]("a"), b=1)
    assert graph.content_hash(params={"a": 2}) == graph.content_hash(params={"a": 2})
    assert graph.content_hash(params={"a": 2}) != graph.content_hash(params={"a": "2"})


def test_cached_step_subclass_warns_and_runs_in_a_pipeline():
    from adag import CachedStep

    with pytest.warns(DeprecationWarning, match="subclass Step"):

        @dataclass(frozen=True)
        class Old(CachedStep[CountResult]):
            def run(self) -> CountResult:
                return CountResult(count=1)

    assert Old.result_type is CountResult
    assert Pipeline(Old()()).execute().count == 1
