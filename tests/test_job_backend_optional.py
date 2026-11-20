"""The optional JobBackend methods (find, logs, close) and Resources set on a fan-out.

The fake backend is the scripted scheduler of ``test_remote_dispatcher``, extended with the three
optional methods. A backend with only submit, status and cancel keeps working: every test in
``test_remote_dispatcher`` runs against one.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from adag import FanOutStep, NullObserver, Pipeline, Step, Template
from adag.cli import load_pipeline_module
from adag.deferred import BranchRef, ResolveContext
from adag.dispatch import (
    LOG_TAIL_CHARS,
    BranchJob,
    BranchPolicy,
    JobState,
    LocalJobBackend,
    RemoteDispatcher,
    Resources,
)
from adag.fan_out import FanOutBranchError
from test_remote_dispatcher import (
    PIPELINE_SOURCE,
    FakeClock,
    FakeJobBackend,
    _dispatcher,  # pyright: ignore[reportPrivateUsage]
    _values,  # pyright: ignore[reportPrivateUsage]
)

P, R, S, F = JobState.PENDING, JobState.RUNNING, JobState.SUCCEEDED, JobState.FAILED


@dataclass
class SchedulerBackend(FakeJobBackend):
    """A scheduler that outlives the dispatcher: it keeps jobs by name, as SLURM or Batch do."""

    names: dict[str, str] = field(default_factory=dict[str, str])
    log_text: dict[str, str] = field(default_factory=dict[str, str])
    find_calls: list[str] = field(default_factory=list[str])
    find_error: Exception | None = None
    close_error: Exception | None = None
    closed: int = 0

    def submit(self, job: BranchJob) -> str:
        job_id = super().submit(job)
        self.names[job.name()] = job_id
        return job_id

    def find(self, name: str) -> str | None:
        self.find_calls.append(name)
        if self.find_error is not None:
            raise self.find_error
        return self.names.get(name)

    def logs(self, job_id: str) -> str | None:
        return self.log_text.get(self._jobs[job_id].job.branch.key)

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline[Any]:
    path = tmp_path / "pipeline.py"
    path.write_text(PIPELINE_SOURCE)
    module = load_pipeline_module(path)
    return Pipeline(module.per_key, cache_dir=tmp_path / "cache")


def _submitted_for(backend: FakeJobBackend, key: str) -> list[int]:
    return [job.attempt for job in backend.submitted if job.branch.key == key]


# find: reattach after a crash


def test_a_rerun_reattaches_to_the_jobs_the_old_dispatcher_left_alive(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(
        script={"a": [[R, R, S]], "b": [[R, R, S]], "c": [[R, R, S]]}, interrupt_on_status=1
    )
    with pytest.raises(KeyboardInterrupt):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))
    assert len(backend.submitted) == 3

    result = pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))

    assert _values(result) == {"a": 2, "b": 4, "c": 6}
    assert len(backend.submitted) == 3  # no duplicate job


def test_a_job_that_already_failed_is_not_reattached(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(script={"a": [[F]]})
    with pytest.raises(FanOutBranchError):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))
    backend.script = {}

    result = pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))

    assert _values(result)["a"] == 2
    assert _submitted_for(backend, "a") == [1, 1]  # a new job, not the failed one


def test_every_attempt_is_probed_and_the_latest_live_job_wins(pipeline: Pipeline[Any]):
    """The job name holds the attempt number: the old run died while attempt 2 was running."""
    backend = SchedulerBackend(
        script={"a": [[F], [R, R, S]]}, interrupt_on_status=3, max_status_calls=100
    )
    policy = {"max_retries": 2}
    with pytest.raises(KeyboardInterrupt):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock(), **policy))
    assert _submitted_for(backend, "a") == [1, 2]
    backend.find_calls.clear()

    result = pipeline.execute(dispatcher=_dispatcher(backend, FakeClock(), **policy))

    assert _values(result)["a"] == 2
    assert _submitted_for(backend, "a") == [1, 2]  # reattached to attempt 2
    probed = [name for name in backend.find_calls if name.endswith(("-1", "-2", "-3"))]
    assert {name.rsplit("-", 1)[1] for name in probed} == {"1", "2", "3"}


def test_a_cached_branch_never_asks_the_backend_to_find_it(pipeline: Pipeline[Any]):
    pipeline.execute(dispatcher=_dispatcher(SchedulerBackend(script={}), FakeClock()))
    backend = SchedulerBackend(script={})

    pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))

    assert backend.find_calls == []
    assert backend.submitted == []


def test_a_failing_find_submits_a_new_job(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(script={}, find_error=RuntimeError("scheduler down"))

    result = pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))

    assert _values(result) == {"a": 2, "b": 4, "c": 6}
    assert len(backend.submitted) == 3


def test_max_concurrent_still_limits_reattached_jobs(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(
        script={"a": [[R, S]], "b": [[R, S]], "c": [[R, S]]}, interrupt_on_status=1
    )
    with pytest.raises(KeyboardInterrupt):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))
    clock = FakeClock()
    dispatcher = RemoteDispatcher(
        backend,
        BranchPolicy(poll_interval_seconds=1.0),
        max_concurrent=1,
        clock=clock,
        sleep=clock.sleep,
    )

    result = pipeline.execute(dispatcher=dispatcher)

    assert _values(result) == {"a": 2, "b": 4, "c": 6}
    assert len(backend.submitted) == 3


# logs


def test_the_end_of_a_failed_jobs_output_is_in_the_error(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(script={"a": [[F]]}, log_text={"a": "x" * 5000 + "ValueError: bad"})

    with pytest.raises(FanOutBranchError) as raised:
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))

    detail = raised.value.failed["a"]
    assert detail.startswith("boom\nlast output of job job-")
    assert detail.endswith("ValueError: bad")
    assert len(detail) < LOG_TAIL_CHARS + 100


def test_output_already_in_the_detail_is_not_repeated(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(script={"a": [[F]]}, log_text={"a": "boom"})

    with pytest.raises(FanOutBranchError) as raised:
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))

    assert raised.value.failed["a"] == "boom"


def test_a_detail_that_ends_the_output_is_replaced_by_the_longer_text(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(script={"a": [[F]]}, log_text={"a": "context line\nboom"})

    with pytest.raises(FanOutBranchError) as raised:
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))

    assert raised.value.failed["a"] == "context line\nboom"


def test_the_observer_gets_the_short_detail(pipeline: Pipeline[Any]):
    seen: list[str] = []

    class Recording(NullObserver):
        def on_fan_out_branch_failed(self, key: str, error: str) -> None:
            seen.append(error)

    backend = SchedulerBackend(script={"a": [[F]]}, log_text={"a": "long output"})
    with pytest.raises(FanOutBranchError):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()), observer=Recording())

    assert seen == ["boom"]


def test_a_retried_attempt_does_not_read_the_logs(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(script={"a": [[F], [S]]}, log_text={"a": "first attempt output"})

    result = pipeline.execute(dispatcher=_dispatcher(backend, FakeClock(), max_retries=1))

    assert _values(result)["a"] == 2


# close


def test_close_is_called_once_per_dispatch_all_and_when_it_raises(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(script={"a": [[F]]})
    with pytest.raises(FanOutBranchError):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))
    assert backend.closed == 1

    backend.script = {}
    pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))
    assert backend.closed == 2


def test_close_runs_after_ctrl_c_cancelled_the_live_jobs(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(script={"a": [[R]]}, interrupt_on_status=1)

    with pytest.raises(KeyboardInterrupt):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))

    assert backend.cancelled  # cancel first, then close
    assert backend.closed == 1


def test_a_failing_close_does_not_hide_the_result(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(script={}, close_error=RuntimeError("server already gone"))

    result = pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))

    assert _values(result) == {"a": 2, "b": 4, "c": 6}
    assert backend.closed == 1


def test_a_failing_close_does_not_replace_the_error(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(script={"a": [[F]]}, close_error=RuntimeError("server already gone"))

    with pytest.raises(FanOutBranchError):
        pipeline.execute(dispatcher=_dispatcher(backend, FakeClock()))


def test_a_run_that_needs_no_store_never_reaches_close():
    """The checks come first: nothing was started, so there is nothing to close."""
    backend = SchedulerBackend(script={})
    dispatcher = RemoteDispatcher(backend)
    with pytest.raises(ValueError, match="requires a cache_dir or store"):
        dispatcher.dispatch_all([], ResolveContext())
    assert backend.closed == 0


# LocalJobBackend


def test_local_backend_keeps_the_stderr_of_a_failed_job_until_close(tmp_path: Path):
    backend = LocalJobBackend()
    ref = BranchRef(str(tmp_path / "missing_pipeline.py"), "var", "k", "0" * 12)
    job_id = backend.submit(BranchJob(ref, "{}", str(tmp_path / "cache")))
    assert backend.logs(job_id) is None  # still running

    deadline = time.monotonic() + 30
    status = backend.status([job_id])[job_id]
    while status.state is JobState.RUNNING and time.monotonic() < deadline:
        time.sleep(0.05)
        status = backend.status([job_id])[job_id]
    assert status.state is JobState.FAILED

    text = backend.logs(job_id)
    assert text
    assert text.strip().endswith(status.detail.strip()[-50:])

    backend.close()
    assert backend.logs(job_id) is None
    backend.close()  # safe to call twice


def test_local_backend_close_stops_a_running_job(tmp_path: Path):
    backend = LocalJobBackend()
    ref = BranchRef(str(tmp_path / "missing_pipeline.py"), "var", "k", "0" * 12)
    backend.submit(BranchJob(ref, "{}", str(tmp_path / "cache")))

    backend.close()

    assert backend._procs == {}  # pyright: ignore[reportPrivateUsage]


# Resources on the fan-out


class Doubled(BaseModel):
    value: int


@dataclass(frozen=True)
class Double(Step[Doubled]):
    n: int

    def run(self, n: int) -> Doubled:
        return Doubled(value=n * 2)


def _double(key: str, n: int) -> Any:
    return Double(n=n)()


def _static(resources: Resources | None) -> Any:
    return FanOutStep[str, Doubled](over={"a": 1, "b": 2}, factory=_double, resources=resources)()


def _template(resources: Resources | None) -> Any:
    return FanOutStep[str, Doubled](
        over={"a": 1, "b": 2}, template=Template(build=_double), resources=resources
    )()


@pytest.mark.parametrize("build", [_static, _template])
def test_resources_do_not_change_any_cache_key(build: Any):
    plain, sized = build(None), build(Resources(cpus=8, memory_mb=4096, gpus=1))

    assert plain.content_hash() == sized.content_hash()
    assert plain.explain_key() == sized.explain_key()


def test_a_fan_out_s_resources_replace_the_dispatchers_default(tmp_path: Path):
    source = PIPELINE_SOURCE.replace(
        'fan_out_var="per_key",',
        'fan_out_var="per_key",\n    resources=Resources(cpus=4, memory_mb=2048),',
    ).replace(
        "from adag.fan_out import FanOutStep",
        "from adag.dispatch import Resources\nfrom adag.fan_out import FanOutStep",
    )
    path = tmp_path / "pipeline.py"
    path.write_text(source)
    module = load_pipeline_module(path)
    pipeline = Pipeline(module.per_key, cache_dir=tmp_path / "cache")
    backend = SchedulerBackend(script={})
    clock = FakeClock()
    dispatcher = RemoteDispatcher(
        backend,
        BranchPolicy(poll_interval_seconds=1.0),
        resources=Resources(cpus=1, gpus=1),
        clock=clock,
        sleep=clock.sleep,
    )

    pipeline.execute(dispatcher=dispatcher)

    assert {job.resources for job in backend.submitted} == {Resources(cpus=4, memory_mb=2048)}


def test_without_resources_on_the_fan_out_the_dispatcher_default_is_used(pipeline: Pipeline[Any]):
    backend = SchedulerBackend(script={})
    clock = FakeClock()
    dispatcher = RemoteDispatcher(
        backend,
        BranchPolicy(poll_interval_seconds=1.0),
        resources=Resources(cpus=1, gpus=1),
        clock=clock,
        sleep=clock.sleep,
    )

    pipeline.execute(dispatcher=dispatcher)

    assert {job.resources for job in backend.submitted} == {Resources(cpus=1, gpus=1)}
