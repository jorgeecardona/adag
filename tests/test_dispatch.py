"""Branch dispatch end to end: a worker resolves a branch, the orchestrator picks it up by hash.

The SubprocessDispatcher tests are the real proof of cache-as-IPC: branches resolve in
separate OS processes, write their results to a shared cache directory keyed by content
hash, and the orchestrator loads them back — no result ever crosses the wire, only the
branch reference. The BatchDispatcher tests do the same against a fake async client.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

import adag.dispatch as dispatch_module
from adag.cli import load_pipeline_module, run_branch
from adag.dispatch import (
    ENV_BRANCH_CACHE_DIR,
    ENV_BRANCH_FAN_OUT,
    ENV_BRANCH_KEY,
    ENV_BRANCH_MODULE,
    ENV_BRANCH_PARAMS,
    BatchDispatcher,
    BranchPolicy,
    SubprocessDispatcher,
)
from adag.fan_out import FanOutBranchError
from adag.pipeline import Pipeline

# A self-contained pipeline file: a module-level FanOutStep over three keys, each branch a
# Double step whose result records the PID it ran in (to prove it ran in a worker process).
PIPELINE_SOURCE = """
import os
from dataclasses import dataclass

from pydantic import BaseModel

from adag.fan_out import FanOutStep
from adag.step import Step


class Doubled(BaseModel):
    key: str
    value: int
    pid: int


@dataclass(frozen=True)
class Double(Step[Doubled]):
    label_key: str
    n: int

    def run(self, label_key: str, n: int) -> Doubled:
        return Doubled(key=label_key, value=n * 2, pid=os.getpid())


def _branch(key, value):
    return Double(label_key=key, n=value)()


per_key = FanOutStep(
    over={"a": 1, "b": 2, "c": 3},
    factory=_branch,
    pipeline_module=__file__,
    fan_out_var="per_key",
)()
"""


def _write_pipeline(tmp_path: Path) -> Path:
    path = tmp_path.joinpath("pipeline.py")
    path.write_text(PIPELINE_SOURCE)
    return path


def test_branch_hash_is_stable_across_rederivation(tmp_path: Path):
    """Orchestrator and worker re-derive the same branch hash — the cache-as-IPC keystone."""
    path = _write_pipeline(tmp_path)
    orchestrator = load_pipeline_module(path)
    worker = load_pipeline_module(path)  # a fresh import, as the worker process would do

    orchestrator_fan: Any = orchestrator.per_key.step
    worker_fan: Any = worker.per_key.step
    orchestrator_hash = orchestrator_fan.factory("a", orchestrator_fan.over["a"]).content_hash()
    worker_hash = worker_fan.factory("a", worker_fan.over["a"]).content_hash()

    assert orchestrator_hash is not None
    assert orchestrator_hash == worker_hash


def test_subprocess_dispatcher_resolves_branches_in_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Each branch resolves in a child process; the orchestrator loads the results by hash."""
    # The dispatcher spawns `python -m adag resolve-branch` in a fresh
    # interpreter, which otherwise resolves adag from site-packages — a
    # possibly-stale install (e.g. the baked CI image). Point PYTHONPATH at the
    # checked-out workspace source so the worker runs the same code as this test.
    repo_root = Path(__file__).resolve().parents[3]
    src_path = os.pathsep.join(str(p) for p in sorted(repo_root.glob("packages/*/src")))
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", f"{src_path}{os.pathsep}{existing}" if existing else src_path)

    path = _write_pipeline(tmp_path)
    cache_dir = tmp_path.joinpath("cache")
    module = load_pipeline_module(path)

    spawns: list[int] = []
    real_run: Any = dispatch_module.subprocess.run

    def counting_run(*args: Any, **kwargs: Any) -> Any:
        spawns.append(1)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(dispatch_module.subprocess, "run", counting_run)

    pipeline: Pipeline[Any] = Pipeline(module.per_key, cache_dir=cache_dir)
    result = pipeline.execute(dispatcher=SubprocessDispatcher())

    assert {key: record.value for key, record in result.items()} == {"a": 2, "b": 4, "c": 6}
    # Every branch ran in a worker process, not the orchestrator.
    assert all(record.pid != os.getpid() for record in result.values())
    assert len(spawns) == 3  # one subprocess per branch

    # Second run: every branch is a cache hit, so nothing is spawned.
    spawns.clear()
    again = pipeline.execute(dispatcher=SubprocessDispatcher())
    assert {key: record.value for key, record in again.items()} == {"a": 2, "b": 4, "c": 6}
    assert len(spawns) == 0


class _FakeJobStatus:
    def __init__(self, status: str, exit_code: int | None) -> None:
        self.status = status
        self.exit_code = exit_code


class _FakeBatchClient:
    """Stand-in for the AWS Batch client: optionally runs the worker on submit to simulate
    a job that lands its result in the shared cache, then reports a terminal status.
    """

    def __init__(self, status: str, run_worker: bool) -> None:
        self.status = status
        self.run_worker = run_worker
        self.submitted: list[dict[str, str]] = []

    async def submit_job(
        self, *, job_name: str, environment: dict[str, str], vcpus: int, memory_mb: int, gpu: bool
    ) -> str:
        self.submitted.append(environment)
        if self.run_worker:
            params = [
                f"{name}={value}"
                for name, value in json.loads(environment[ENV_BRANCH_PARAMS]).items()
            ]
            run_branch(
                Path(environment[ENV_BRANCH_MODULE]),
                environment[ENV_BRANCH_FAN_OUT],
                environment[ENV_BRANCH_KEY],
                params,
                Path(environment[ENV_BRANCH_CACHE_DIR]),
            )
        return f"job-{len(self.submitted)}"

    async def describe_job(self, job_id: str) -> _FakeJobStatus:
        return _FakeJobStatus(self.status, 0 if self.status == "SUCCEEDED" else 1)


def test_batch_dispatcher_submits_polls_and_loads(tmp_path: Path):
    """One job per uncached branch; on SUCCEEDED the result is loaded from the shared cache."""
    path = _write_pipeline(tmp_path)
    cache_dir = tmp_path.joinpath("cache")
    module = load_pipeline_module(path)
    client = _FakeBatchClient(status="SUCCEEDED", run_worker=True)

    pipeline: Pipeline[Any] = Pipeline(module.per_key, cache_dir=cache_dir)
    result = pipeline.execute(
        dispatcher=BatchDispatcher(client, gpu=False, policy=BranchPolicy(poll_interval_seconds=0))
    )

    assert {key: record.value for key, record in result.items()} == {"a": 2, "b": 4, "c": 6}
    assert len(client.submitted) == 3  # one Batch job per branch


def test_batch_dispatcher_failed_job_raises(tmp_path: Path):
    """A FAILED job surfaces as a FanOutBranchError naming the failed branches."""
    path = _write_pipeline(tmp_path)
    cache_dir = tmp_path.joinpath("cache")
    module = load_pipeline_module(path)
    client = _FakeBatchClient(status="FAILED", run_worker=False)

    pipeline: Pipeline[Any] = Pipeline(module.per_key, cache_dir=cache_dir)
    with pytest.raises(FanOutBranchError):
        pipeline.execute(
            dispatcher=BatchDispatcher(
                client, gpu=False, policy=BranchPolicy(poll_interval_seconds=0)
            )
        )
