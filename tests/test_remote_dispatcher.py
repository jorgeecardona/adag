"""RemoteDispatcher against a scripted fake backend: retries, timeouts, cancel, reruns.

``FakeJobBackend`` plays a scheduler. Each branch key has a script: one list of states per
attempt, one state per poll (the last state repeats). When a job reaches SUCCEEDED the fake
runs the real worker entry, ``run_branch``, in-process, so results land in the cache exactly as
a remote worker would write them. The clock is fake, so no test waits.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from adag.cli import load_pipeline_module, run_branch
from adag.dispatch import (
    EXIT_BAD_BRANCH,
    BranchJob,
    BranchPolicy,
    JobState,
    JobStatus,
    RemoteDispatcher,
)
from adag.fan_out import FanOutBranchError
from adag.pipeline import Pipeline

P, R, S, F = JobState.PENDING, JobState.RUNNING, JobState.SUCCEEDED, JobState.FAILED

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
        return Doubled(value=n * 2)


per_key = FanOutStep(
    over={"a": 1, "b": 2, "c": 3},
    factory=lambda key, n: Double(n=n)(),
    pipeline_module=__file__,
    fan_out_var="per_key",
)()
"""


@dataclass
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 1.0)  # every poll moves time forward


@dataclass
class _FakeJob:
    job: BranchJob
    states: list[JobState]
    polls: int = 0
    done: bool = False


@dataclass
class FakeJobBackend:
    """A scheduler that follows a script. Records every submit, status and cancel call."""

    script: dict[str, list[list[JobState]]]
    late_result: bool = False
    exit_code: int = 1
    submitted: list[BranchJob] = field(default_factory=list[BranchJob])
    status_calls: int = 0
    cancelled: list[str] = field(default_factory=list[str])
    interrupt_on_status: int | None = None
    _jobs: dict[str, _FakeJob] = field(default_factory=dict[str, _FakeJob])

    def submit(self, job: BranchJob) -> str:
        self.submitted.append(job)
        attempts = self.script.get(job.branch.key, [[S]])
        states = attempts[min(job.attempt, len(attempts)) - 1]
        job_id = f"job-{len(self.submitted)}"
        self._jobs[job_id] = _FakeJob(job, states)
        return job_id

    def status(self, job_ids: Sequence[str]) -> Mapping[str, JobStatus]:
        self.status_calls += 1
        if self.interrupt_on_status == self.status_calls:
            raise KeyboardInterrupt
        out: dict[str, JobStatus] = {}
        for job_id in job_ids:
            fake = self._jobs[job_id]
            state = fake.states[min(fake.polls, len(fake.states) - 1)]
            fake.polls += 1
            if state is S and not fake.done:
                if self.late_result and fake.polls == 1:
                    out[job_id] = JobStatus(S)  # result not visible yet on this poll
                    fake.states = [S]
                    fake.polls = 0
                    self.late_result = False
                    continue
                self._run_worker(fake.job)
                fake.done = True
            exit_code = self.exit_code if state is F else None
            out[job_id] = JobStatus(state, "boom" if state is F else "", exit_code)
        return out

    def cancel(self, job_ids: Sequence[str]) -> None:
        self.cancelled.extend(job_ids)

    def _run_worker(self, job: BranchJob) -> None:
        run_branch(
            job.branch.pipeline_module,
            job.branch.fan_out_var,
            job.branch.key,
            {},
            Path(job.cache_dir),
            expect_hash=job.branch.branch_hash,
        )


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline[Any]:
    path = tmp_path / "pipeline.py"
    path.write_text(PIPELINE_SOURCE)
    module = load_pipeline_module(path)
    return Pipeline(module.per_key, cache_dir=tmp_path / "cache")


def _dispatcher(backend: FakeJobBackend, clock: FakeClock, **policy: Any) -> RemoteDispatcher:
    return RemoteDispatcher(
        backend,
        BranchPolicy(poll_interval_seconds=1.0, **policy),
        clock=clock,
        sleep=clock.sleep,
    )


def _values(result: dict[str, Any]) -> dict[str, int]:
    return {key: record.value for key, record in result.items()}


def test_all_branches_succeed(pipeline: Pipeline[Any]):
    backend = FakeJobBackend(script={"a": [[P, R, S]], "b": [[R, S]], "c": [[S]]})
    clock = FakeClock()
    result = pipeline.execute(dispatcher=_dispatcher(backend, clock))
    assert _values(result) == {"a": 2, "b": 4, "c": 6}
    assert backend.cancelled == []


def test_failed_job_is_retried_up_to_max_retries_then_raises(pipeline: Pipeline[Any]):
    backend = FakeJobBackend(script={"a": [[F], [F], [F]]})
    with pytest.raises(FanOutBranchError) as caught:
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock(), max_retries=2))
    assert caught.value.failed == {"a": "boom"}
    assert sorted(caught.value.succeeded) == ["b", "c"]
    assert [job.attempt for job in backend.submitted if job.branch.key == "a"] == [1, 2, 3]


def test_retry_succeeds_on_a_later_attempt(pipeline: Pipeline[Any]):
    backend = FakeJobBackend(script={"a": [[F], [S]]})
    result = pipeline.execute(dispatcher=_dispatcher(backend, FakeClock(), max_retries=1))
    assert _values(result)["a"] == 2


def test_bad_branch_exit_code_is_not_retried(pipeline: Pipeline[Any]):
    backend = FakeJobBackend(script={"a": [[F], [S]]}, exit_code=EXIT_BAD_BRANCH)
    with pytest.raises(FanOutBranchError):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock(), max_retries=3))
    assert [job.attempt for job in backend.submitted if job.branch.key == "a"] == [1]


def test_timeout_counts_from_running_not_from_submit(pipeline: Pipeline[Any]):
    """Ten polls in the queue do not use up a 3 s run timeout."""
    backend = FakeJobBackend(script={"a": [[P] * 10 + [R, R, S]]})
    result = pipeline.execute(dispatcher=_dispatcher(backend, FakeClock(), timeout_seconds=3))
    assert _values(result)["a"] == 2


def test_run_timeout_cancels_and_fails(pipeline: Pipeline[Any]):
    backend = FakeJobBackend(script={"a": [[R]]})
    with pytest.raises(FanOutBranchError, match="timed out"):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock(), timeout_seconds=3))
    assert "job-1" in backend.cancelled


def test_queue_timeout_cancels_and_fails(pipeline: Pipeline[Any]):
    backend = FakeJobBackend(script={"a": [[P]]})
    with pytest.raises(FanOutBranchError, match="still queued"):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock(), queue_timeout_seconds=5))


def test_ctrl_c_cancels_every_live_job(pipeline: Pipeline[Any]):
    backend = FakeJobBackend(script={"a": [[R]], "b": [[R]], "c": [[R]]}, interrupt_on_status=2)
    with pytest.raises(KeyboardInterrupt):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))
    assert sorted(backend.cancelled) == ["job-1", "job-2", "job-3"]


def test_succeeded_with_a_late_result_is_not_a_failure(pipeline: Pipeline[Any]):
    backend = FakeJobBackend(script={"a": [[S]]}, late_result=True)
    result = pipeline.execute(dispatcher=_dispatcher(backend, FakeClock(), result_grace_seconds=10))
    assert _values(result)["a"] == 2
    assert len(backend.submitted) == 3  # no resubmit for the late branch


def test_cache_hit_submits_nothing(pipeline: Pipeline[Any]):
    pipeline.execute(dispatcher=_dispatcher(FakeJobBackend(script={}), FakeClock()))
    backend = FakeJobBackend(script={})
    pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))
    assert backend.submitted == []
    assert backend.status_calls == 0


def test_many_jobs_cost_one_status_call_per_poll(pipeline: Pipeline[Any]):
    backend = FakeJobBackend(script={"a": [[R, R, S]], "b": [[R, R, S]], "c": [[R, R, S]]})
    pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))
    assert backend.status_calls == 3


def test_max_concurrent_limits_live_jobs(pipeline: Pipeline[Any]):
    backend = FakeJobBackend(script={})
    clock = FakeClock()
    dispatcher = RemoteDispatcher(
        backend,
        BranchPolicy(poll_interval_seconds=1),
        max_concurrent=1,
        clock=clock,
        sleep=clock.sleep,
    )
    result = pipeline.execute(dispatcher=dispatcher)
    assert _values(result) == {"a": 2, "b": 4, "c": 6}
    assert backend.status_calls == 3  # one job at a time


def test_rerun_after_partial_failure_submits_only_failed_keys(pipeline: Pipeline[Any]):
    with pytest.raises(FanOutBranchError):
        pipeline.execute(dispatcher=_dispatcher(FakeJobBackend(script={"b": [[F]]}), FakeClock()))
    backend = FakeJobBackend(script={})
    result = pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))
    assert _values(result) == {"a": 2, "b": 4, "c": 6}
    assert [job.branch.key for job in backend.submitted] == ["b"]
