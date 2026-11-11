"""Whole-DAG observation via PipelineObserver lifecycle hooks, and the run tree.

resolve() emits on_step_start / on_step_cached / on_step_finished for every step it reaches,
through context.observer. NullObserver no-ops them, so existing observers (e.g. the inference
observers) that only care about one hook keep working by inheriting NullObserver.
RunTreeObserver reconstructs the execution as an indented tree from the start/finish nesting —
the depth-first resolve order brackets each child's events inside its parent's.
"""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from structlog.testing import capture_logs

from adag.deferred import NullObserver
from adag.deprecation import AdagDeprecationWarning
from adag.fan_out import FanOutStep
from adag.observers import RunTreeObserver
from adag.pipeline import Pipeline
from adag.step import Step


class Number(BaseModel):
    """Minimal result model."""

    value: int


@dataclass(frozen=True)
class Start(Step[Number]):
    """Leaf step producing a number."""

    n: int

    def run(self, n: int) -> Number:
        return Number(value=n)


@dataclass(frozen=True)
class Double(Step[Number]):
    """Doubles its upstream number."""

    def run(self, source: Number) -> Number:
        return Number(value=source.value * 2)


class RecordingObserver(NullObserver):
    """Captures the lifecycle event stream (kind, step) for assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def on_step_start(self, step: str) -> None:
        self.events.append(("start", step))

    def on_step_cached(self, step: str) -> None:
        self.events.append(("cached", step))

    def on_step_finished(self, step: str, seconds: float) -> None:
        self.events.append(("finished", step))


def build_two_step_graph():
    """Start(3) -> Double, terminal value 6."""
    return Double()(source=Start(n=3)(n=3))


def test_observer_sees_step_lifecycle_depth_first():
    """Each child's start/finished is bracketed inside its parent's — the nesting the tree needs."""
    observer = RecordingObserver()
    result = Pipeline(build_two_step_graph()).execute(observer=observer)

    assert result.value == 6
    assert observer.events == [
        ("start", "Double"),
        ("start", "Start"),
        ("finished", "Start"),
        ("finished", "Double"),
    ]


def test_observer_cache_hit_emits_cached_not_finished(tmp_path: Path):
    """A cache hit emits on_step_cached and no on_step_finished for that step."""
    graph = build_two_step_graph()
    Pipeline(graph, cache_dir=tmp_path).execute()  # populate cache

    observer = RecordingObserver()
    Pipeline(graph, cache_dir=tmp_path).execute(observer=observer)

    assert ("cached", "Double") in observer.events
    assert all(kind != "finished" for kind, _ in observer.events)


def test_single_hook_observer_still_works():
    """An observer that overrides only on_step_start must not break — NullObserver no-ops the rest."""

    class StartOnly(NullObserver):
        def __init__(self) -> None:
            self.started: list[str] = []

        def on_step_start(self, step: str) -> None:
            self.started.append(step)

    observer = StartOnly()
    result = Pipeline(build_two_step_graph()).execute(observer=observer)
    assert result.value == 6
    assert observer.started


def test_default_observer_logs_step_lifecycle():
    """With no observer, the default LoggingObserver logs each step's lifecycle — the single source of per-step logs."""
    with capture_logs() as logs:
        Pipeline(build_two_step_graph()).execute()

    events = [entry["event"] for entry in logs]
    assert "step.start" in events
    assert "step.finished" in events


def test_null_observer_silences_step_logging():
    """Passing NullObserver is the one way to turn per-step logging off."""
    with capture_logs() as logs:
        Pipeline(build_two_step_graph()).execute(observer=NullObserver())

    events = [entry["event"] for entry in logs]
    assert "step.start" not in events
    assert "step.finished" not in events


def test_run_tree_observer_renders_nested_tree():
    """RunTreeObserver renders the run as an indented tree: root, then nested children."""
    observer = RunTreeObserver()
    Pipeline(build_two_step_graph()).execute(observer=observer)

    lines = observer.render_tree().splitlines()
    assert lines[0].startswith("Double")
    assert "Start" in lines[1]
    assert lines[1].lstrip().startswith(("└─", "├─")) or lines[1].startswith((" ", "└", "├"))


def test_run_tree_observer_labels_fan_out_branches_with_keys():
    """Fan-out branches are labelled with their key so identical step types are distinguishable."""
    fan = FanOutStep(
        over={"alpha": 1, "beta": 2}, factory=lambda key, value: Start(n=value)(n=value)
    )()
    observer = RunTreeObserver()
    Pipeline(fan).execute(observer=observer)

    tree = observer.render_tree()
    assert "Start [alpha]" in tree
    assert "Start [beta]" in tree


def test_null_observer_silences_a_cached_batch_run(tmp_path: Path):
    """With NullObserver, a run that saves, caches and batches logs nothing at info level."""
    from adag import BatchItem, BatchStep, InputParam

    class Item(BaseModel):
        v: int

    class Items(BaseModel):
        vs: list[int]

    @dataclass(frozen=True)
    class Batch(BatchStep[Items, Item]):
        def items(self, n: int) -> list[BatchItem]:
            return [BatchItem(key=str(i), data=i) for i in range(n)]

        def process_item(self, item: BatchItem, n: int) -> Item:
            return Item(v=item.data)

        def assemble(self, results: list[Item], n: int) -> Items:
            return Items(vs=[r.v for r in results])

    pipeline = Pipeline(Batch()(n=InputParam[int]("n")), cache_dir=tmp_path)
    with capture_logs() as logs:
        pipeline.execute(observer=NullObserver(), n=3)
        pipeline.execute(observer=NullObserver(), n=3)
    assert [entry for entry in logs if entry["log_level"] in {"info", "debug"}] == []


def test_default_observer_logs_progress_events(tmp_path: Path):
    """The default LoggingObserver turns progress events (step.saved) into log lines."""
    with capture_logs() as logs:
        Pipeline(build_two_step_graph(), cache_dir=tmp_path).execute()
    assert "step.saved" in [entry["event"] for entry in logs]


def test_protocol_only_observer_without_on_event_completes_a_run(tmp_path: Path) -> None:
    """An observer written before ``on_event`` existed (no NullObserver base) still works."""
    from adag import Pipeline

    class OldObserver:
        def __init__(self) -> None:
            self.started: list[str] = []

        def on_step_start(self, step: str) -> None:
            self.started.append(step)

        def on_step_cached(self, step: str) -> None: ...

        def on_step_finished(self, step: str, seconds: float) -> None: ...

        def on_fan_out_branch(self, key: str) -> None: ...

        def on_fan_out_branch_failed(self, key: str, error: str) -> None: ...

    observer = OldObserver()
    pipeline = Pipeline(_compat_step()(), cache_dir=tmp_path)
    pipeline.execute(observer=observer)  # pyright: ignore[reportArgumentType]
    assert observer.started


def test_deprecated_domain_hooks_are_no_ops_that_warn_once() -> None:
    import warnings

    from adag import LoggingObserver, deferred

    deferred._WARNED_DOMAIN_HOOKS.clear()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        LoggingObserver().on_embedding([0.1])
        LoggingObserver().on_embedding([0.2])
        LoggingObserver().on_model_refused("m")
    messages = [str(w.message) for w in caught if w.category is AdagDeprecationWarning]
    assert len(messages) == 2
    assert "on_embedding" in messages[0] and "on_model_refused" in messages[1]


def _compat_step():
    from dataclasses import dataclass

    from pydantic import BaseModel

    from adag import Step

    class Value(BaseModel):
        n: int

    @dataclass(frozen=True)
    class One(Step[Value]):
        def run(self) -> Value:
            return Value(n=1)

    return One()
