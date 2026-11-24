"""Worker events: a dispatched branch reports through the store while it runs."""

import contextlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from adag import NullObserver
from adag._worker_events import WorkerEvent, WorkerEventObserver, WorkerRecord, events_key
from adag.cache_store import FileSystemCacheStore, MemoryCacheStore
from adag.cli import load_pipeline_module, run_branch
from adag.dispatch import BranchJob, BranchPolicy, JobState, JobStatus, RemoteDispatcher
from adag.fan_out import FanOutBranchError
from adag.pipeline import Pipeline

PIPELINE_SOURCE = """
from dataclasses import dataclass

from pydantic import BaseModel

from adag.fan_out import FanOutStep
from adag.step import Step


class Doubled(BaseModel):
    value: int


@dataclass(frozen=True)
class Double(Step[Doubled]):
    n: int

    def run(self, n: int) -> Doubled:
        if n == 2:
            raise RuntimeError("the model ran out of memory")
        return Doubled(value=n * 2)


per_key = FanOutStep(
    over={"a": 1, "b": 2},
    factory=lambda key, n: Double(n=n)(),
    pipeline_module=__file__,
    fan_out_var="per_key",
)()
"""


def test_a_record_reads_back_as_written() -> None:
    record = WorkerRecord("t", 3, (WorkerEvent(1, "step_start", {"step": "Double"}),))
    assert WorkerRecord.from_json(record.to_json()) == record


@pytest.mark.parametrize(
    "text", ["", "not json", "[]", '{"token": "t"}', '{"token":"t","beat":"x"}']
)
def test_text_that_is_not_a_record_reads_as_none(text: str) -> None:
    assert WorkerRecord.from_json(text) is None


def _fail_after(
    store: MemoryCacheStore, branch_hash: str, body: Callable[[WorkerEventObserver], None]
) -> None:
    """Run ``body`` in an observer block that ends with an error, so the record stays."""
    with (
        contextlib.suppress(ValueError),
        WorkerEventObserver(store, branch_hash, heartbeat_seconds=60, gap_seconds=0.01) as observer,
    ):
        body(observer)
        raise ValueError


def test_the_observer_writes_each_event_and_the_last_ones_when_the_block_fails() -> None:
    store = MemoryCacheStore()

    def body(observer: WorkerEventObserver) -> None:
        observer.on_step_start("Double")
        observer.on_step_finished("Double", 0.5)
        observer.on_event("batch.progress", {"done": 2})

    _fail_after(store, "h1", body)
    text = store.get(events_key("h1"))
    assert text is not None
    record = WorkerRecord.from_json(text)
    assert record is not None
    assert [e.event for e in record.events] == ["step_start", "step_finished", "event"]
    assert record.events[2].fields == {"name": "batch.progress", "fields": {"done": 2}}
    assert [e.seq for e in record.events] == [1, 2, 3]


def test_a_clean_exit_deletes_the_record() -> None:
    store = MemoryCacheStore()
    with WorkerEventObserver(store, "h6", heartbeat_seconds=60, gap_seconds=0.01) as observer:
        observer.on_step_start("Double")
    assert store.get(events_key("h6")) is None


def test_a_live_worker_keeps_writing_when_it_has_no_events() -> None:
    store = MemoryCacheStore()
    with WorkerEventObserver(store, "h2", heartbeat_seconds=0.02, gap_seconds=0.01):
        time.sleep(0.2)
        text = store.get(events_key("h2"))
    record = WorkerRecord.from_json(text or "")
    assert record is not None
    assert record.beat >= 3


def test_failed_is_written_when_the_block_ends() -> None:
    store = MemoryCacheStore()
    _fail_after(store, "h3", lambda observer: observer.failed(RuntimeError("boom")))
    record = WorkerRecord.from_json(store.get(events_key("h3")) or "")
    assert record is not None
    assert record.events[-1].event == "failed"
    assert record.events[-1].fields == {"error_type": "RuntimeError", "error": "boom"}


def test_data_that_json_cannot_hold_does_not_fail_the_callback() -> None:
    store = MemoryCacheStore()
    loop: dict[str, Any] = {}
    loop["me"] = loop
    _fail_after(store, "h7", lambda observer: observer.on_event("x", loop))
    record = WorkerRecord.from_json(store.get(events_key("h7")) or "")
    assert record is not None
    assert "repr" in record.events[-1].fields


def test_a_slow_store_does_not_stall_a_callback() -> None:
    class Slow(MemoryCacheStore):
        def set(self, key: str, value: str) -> None:
            time.sleep(0.3)
            super().set(key, value)

    with WorkerEventObserver(Slow(), "h8", heartbeat_seconds=60, gap_seconds=0.01) as observer:
        started = time.monotonic()
        for i in range(20):
            observer.on_event("tick", {"i": i})
        assert time.monotonic() - started < 0.2


def test_a_store_that_fails_does_not_fail_the_worker() -> None:
    class Broken(MemoryCacheStore):
        def set(self, key: str, value: str) -> None:
            raise OSError("disk full")

    with WorkerEventObserver(Broken(), "h4", gap_seconds=0.01) as observer:
        observer.on_step_start("Double")


def test_the_record_keeps_only_the_newest_events() -> None:
    store = MemoryCacheStore()

    def body(observer: WorkerEventObserver) -> None:
        for i in range(250):
            observer.on_event("tick", {"i": i})

    _fail_after(store, "h5", body)
    record = WorkerRecord.from_json(store.get(events_key("h5")) or "")
    assert record is not None
    assert len(record.events) == 200
    assert record.events[-1].seq == 250


@dataclass
class Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 1.0)


@dataclass
class Watcher(NullObserver):
    events: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )

    def on_event(self, event: str, fields: dict[str, Any]) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


@dataclass
class Scheduler:
    """A fake backend. ``script`` has one state list per branch key. A job that reaches SUCCEEDED
    runs the real worker. ``beats`` makes a running job write a fresh record on each status call,
    as a live worker does."""

    script: dict[str, list[JobState]]
    beats: bool = False
    beat_polls: int | None = None  # stop writing after this many polls, as a dead machine does
    zombie: bool = False  # at submit, a worker of an earlier attempt writes a record
    submitted: list[BranchJob] = field(default_factory=list[BranchJob])
    cancelled: list[str] = field(default_factory=list[str])
    _polls: dict[str, int] = field(default_factory=dict[str, int])
    _jobs: dict[str, BranchJob] = field(default_factory=dict[str, BranchJob])

    def submit(self, job: BranchJob) -> str:
        self.submitted.append(job)
        job_id = f"job-{len(self.submitted)}"
        self._jobs[job_id] = job
        if self.zombie:
            old = WorkerRecord("zombie", 5, (WorkerEvent(1, "step_start", {"step": "Old"}),))
            store = FileSystemCacheStore(Path(job.cache_dir or ""))
            store.set(events_key(job.branch.branch_hash), old.to_json())
        return job_id

    def status(self, job_ids: Sequence[str]) -> Mapping[str, JobStatus]:
        out: dict[str, JobStatus] = {}
        for job_id in job_ids:
            job = self._jobs[job_id]
            n = self._polls.get(job_id, 0)
            self._polls[job_id] = n + 1
            states = self.script.get(job.branch.key, [JobState.SUCCEEDED])
            state = states[min(n, len(states) - 1)]
            if self.beats and state is JobState.RUNNING and n < (self.beat_polls or n + 1):
                store = FileSystemCacheStore(Path(job.cache_dir or ""))
                record = WorkerRecord("alive", n + 1, ())
                store.set(events_key(job.branch.branch_hash), record.to_json())
            if state in (JobState.SUCCEEDED, JobState.FAILED) and n == len(states) - 1:
                self._run_worker(job)
                out[job_id] = JobStatus(JobState.SUCCEEDED)
                continue
            out[job_id] = JobStatus(state)
        return out

    def cancel(self, job_ids: Sequence[str]) -> None:
        self.cancelled.extend(job_ids)

    def _run_worker(self, job: BranchJob) -> None:
        with contextlib.suppress(RuntimeError):  # the job ends; the dispatcher finds no result
            run_branch(
                job.branch.pipeline_module,
                job.branch.fan_out_var,
                job.branch.key,
                {},
                Path(job.cache_dir or ""),
                expect_hash=job.branch.branch_hash,
            )


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline[Any]:
    path = tmp_path / "pipeline.py"
    path.write_text(PIPELINE_SOURCE)
    return Pipeline(load_pipeline_module(path).per_key, cache_dir=tmp_path / "cache")


def _dispatcher(backend: Scheduler, clock: Clock, **policy: Any) -> RemoteDispatcher:
    return RemoteDispatcher(
        backend,
        BranchPolicy(poll_interval_seconds=1.0, result_grace_seconds=0, **policy),
        clock=clock,
        sleep=clock.sleep,
    )


def test_the_dispatcher_forwards_worker_events_to_the_observer(
    pipeline: Pipeline[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dispatcher usually reads before the worker ends. Here the worker ends inside one status
    # call, so it leaves its last record, as a reader that came just before the end would see it.
    def leave_record(self: WorkerEventObserver) -> None:
        self._write()  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(WorkerEventObserver, "_delete", leave_record)
    watcher = Watcher()
    backend = Scheduler(script={"a": [JobState.RUNNING, JobState.SUCCEEDED]})
    with pytest.raises(FanOutBranchError):
        pipeline.execute(dispatcher=_dispatcher(backend, Clock()), observer=watcher)
    names = watcher.names()
    assert "branch.step_start" in names
    assert "branch.step_finished" in names
    start = next(f for n, f in watcher.events if n == "branch.step_start" and f["key"] == "a")
    assert start["attempt"] == 1
    assert start["step"] == "Double"


def test_a_worker_error_reaches_the_failure_detail_and_the_observer(
    pipeline: Pipeline[Any],
) -> None:
    watcher = Watcher()
    backend = Scheduler(script={})
    with pytest.raises(FanOutBranchError) as caught:
        pipeline.execute(dispatcher=_dispatcher(backend, Clock()), observer=watcher)
    assert "RuntimeError: the model ran out of memory" in caught.value.failed["b"]
    failed = [f for n, f in watcher.events if n == "branch.failed"]
    assert failed == [
        {
            "key": "b",
            "attempt": 1,
            "error_type": "RuntimeError",
            "error": "the model ran out of memory",
        }
    ]


def test_the_record_is_deleted_after_a_result_and_kept_after_a_failure(
    pipeline: Pipeline[Any], tmp_path: Path
) -> None:
    backend = Scheduler(script={})
    with pytest.raises(FanOutBranchError):
        pipeline.execute(dispatcher=_dispatcher(backend, Clock()))
    store = FileSystemCacheStore(tmp_path / "cache")
    kept = {
        job.branch.key: store.exists(events_key(job.branch.branch_hash))
        for job in backend.submitted
    }
    assert kept == {"a": False, "b": True}


def test_a_worker_that_goes_quiet_is_cancelled_and_fails(pipeline: Pipeline[Any]) -> None:
    backend = Scheduler(
        script={"a": [JobState.RUNNING], "b": [JobState.RUNNING]}, beats=True, beat_polls=2
    )
    with pytest.raises(FanOutBranchError, match="wrote nothing for 5"):
        pipeline.execute(dispatcher=_dispatcher(backend, Clock(), silent_timeout_seconds=5))
    assert sorted(backend.cancelled) == ["job-1", "job-2"]


def test_a_worker_that_never_wrote_is_not_called_silent(pipeline: Pipeline[Any]) -> None:
    """A slow start is the business of timeout_seconds."""
    states = [JobState.RUNNING] * 20 + [JobState.SUCCEEDED]
    backend = Scheduler(script={"a": states, "b": [JobState.SUCCEEDED]})
    with pytest.raises(FanOutBranchError) as caught:
        pipeline.execute(dispatcher=_dispatcher(backend, Clock(), silent_timeout_seconds=5))
    assert list(caught.value.failed) == ["b"]
    assert backend.cancelled == []


def test_a_worker_that_keeps_writing_is_not_silent(pipeline: Pipeline[Any]) -> None:
    states = [JobState.RUNNING] * 12 + [JobState.SUCCEEDED]
    backend = Scheduler(script={"a": states, "b": [JobState.SUCCEEDED]}, beats=True)
    with pytest.raises(FanOutBranchError) as caught:
        pipeline.execute(dispatcher=_dispatcher(backend, Clock(), silent_timeout_seconds=5))
    assert list(caught.value.failed) == ["b"]  # b raises in its body; a was never called silent
    assert backend.cancelled == []


def test_without_a_silent_timeout_a_silent_worker_is_left_alone(pipeline: Pipeline[Any]) -> None:
    states = [JobState.RUNNING] * 20 + [JobState.SUCCEEDED]
    backend = Scheduler(script={"a": states, "b": [JobState.SUCCEEDED]})
    with pytest.raises(FanOutBranchError) as caught:
        pipeline.execute(dispatcher=_dispatcher(backend, Clock()))
    assert list(caught.value.failed) == ["b"]


def test_each_attempt_reports_on_its_own(pipeline: Pipeline[Any]) -> None:
    """The record of a failed attempt is deleted before the next one, so its token is not adopted."""
    watcher = Watcher()
    backend = Scheduler(script={})
    with pytest.raises(FanOutBranchError):
        pipeline.execute(dispatcher=_dispatcher(backend, Clock(), max_retries=1), observer=watcher)
    attempts = [f["attempt"] for n, f in watcher.events if n == "branch.failed"]
    assert attempts == [1, 2]


def test_a_leftover_worker_does_not_hide_the_real_one(
    pipeline: Pipeline[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def leave_record(self: WorkerEventObserver) -> None:
        self._write()  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(WorkerEventObserver, "_delete", leave_record)
    watcher = Watcher()
    backend = Scheduler(script={"a": [JobState.RUNNING, JobState.SUCCEEDED]}, zombie=True)
    with pytest.raises(FanOutBranchError):
        pipeline.execute(dispatcher=_dispatcher(backend, Clock()), observer=watcher)
    steps = {f["step"] for n, f in watcher.events if n == "branch.step_start" and f["key"] == "a"}
    assert steps == {"Old", "Double"}
