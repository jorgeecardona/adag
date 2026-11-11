"""Branch dispatchers — resolve fan-out branches on workers, rejoin via the cache.

A ``BranchDispatcher`` (see ``deferred.py``) decides *how* a ``FanOutStep`` branch
is resolved. The default ``InlineDispatcher`` runs it in-process.
``SubprocessDispatcher`` runs each branch in a worker process that re-imports the pipeline, re-derives the branch, resolves it,
and writes the result to the shared cache keyed by the branch content hash. The
orchestrator then loads it back by that same hash: the cache is the wire, so
nothing but ``(module, fan_out_var, key, params, cache_dir)`` ever crosses it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

import structlog

from adag.deferred import BranchRef, ResolveContext
from adag.step import StepResult

logger = structlog.get_logger()


@dataclass(frozen=True)
class BranchPolicy:
    """How hard to try a branch and how patiently to wait for it."""

    max_retries: int = 0
    timeout_seconds: float | None = None
    poll_interval_seconds: float = 5.0


EXIT_BAD_BRANCH = 2
"""Worker exit code for a bad branch reference or a hash mismatch. Never retried."""


class BranchReferenceError(LookupError):
    """The worker cannot find the branch, or derives a different hash than the orchestrator."""


class BranchExecutionError(RuntimeError):
    """A single branch's worker failed (nonzero exit, timeout, or job FAILED)."""

    def __init__(self, key: str, detail: str) -> None:
        self.key = key
        super().__init__(f"branch {key!r} failed: {detail}")


@dataclass(frozen=True)
class Resources:
    """What one branch job asks the scheduler for. ``None`` means the scheduler's default."""

    cpus: float | None = None
    memory_mb: int | None = None
    gpus: int = 0


@dataclass(frozen=True)
class BranchJob:
    """One attempt at one fan-out branch, as a backend submits it.

    A backend runs ``command()`` in an environment that has the pipeline code and can reach
    ``cache_dir``. Success means the result is in the cache under ``branch.branch_hash``; the
    job's exit code alone is not trusted.
    """

    branch: BranchRef
    params_json: str
    cache_dir: str
    attempt: int = 1
    resources: Resources = Resources()

    def command(self, prefix: Sequence[str] = ("adag",)) -> tuple[str, ...]:
        """The worker command line. ``prefix`` is how to start adag (``python -m adag``)."""
        return (
            *prefix,
            "resolve-branch",
            self.branch.pipeline_module,
            "--fan-out",
            self.branch.fan_out_var,
            "--key",
            self.branch.key,
            "--cache-dir",
            self.cache_dir,
            "--params-json",
            self.params_json,
            "--expect-hash",
            self.branch.branch_hash,
        )

    def name(self) -> str:
        """A job name unique per branch and attempt, for scheduler consoles."""
        return f"adag-{self.branch.branch_hash}-{self.attempt}"


class JobState(Enum):
    """Where a submitted job is. ``SUCCEEDED`` and ``FAILED`` are final."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class JobStatus:
    """A job's state, with ``detail`` (an error or exit code) for ``FAILED``.

    ``exit_code`` is set when the backend knows it. ``EXIT_BAD_BRANCH`` is never retried.
    """

    state: JobState
    detail: str = ""
    exit_code: int | None = None


class JobBackend(Protocol):
    """Runs branch jobs on some scheduler: a local process, AWS Batch, Kubernetes, SLURM.

    ``RemoteDispatcher`` owns the cache check, retries, timeouts and cancel. A backend only
    starts jobs and reports on them. It must turn off the scheduler's own retries, or the
    attempts multiply.
    """

    def submit(self, job: BranchJob) -> str:
        """Start ``job`` and return its job id."""
        ...

    def status(self, job_ids: Sequence[str]) -> Mapping[str, JobStatus]:
        """Report every job in ``job_ids``, in one call to the scheduler where it can."""
        ...

    def cancel(self, job_ids: Sequence[str]) -> None:
        """Stop the jobs. Jobs already finished are ignored."""
        ...


def params_json(params: Mapping[str, Any]) -> str:
    """Render run params as one JSON object, so the worker gets the same types.

    Raises:
        TypeError: a param value is not JSON-serializable, so it cannot reach a worker.
    """
    try:
        return json.dumps(params, sort_keys=True)
    except TypeError as error:
        raise TypeError(f"dispatch params must be JSON-serializable: {error}") from error


def _param_args(params: dict[str, Any]) -> list[str]:
    """Render run params as one ``--params-json`` arg, so the worker gets the same types.

    Raises:
        TypeError: a param value is not JSON-serializable, so it cannot reach a worker.
    """
    try:
        payload = json.dumps(params, sort_keys=True)
    except TypeError as error:
        raise TypeError(
            f"SubprocessDispatcher params must be JSON-serializable: {error}"
        ) from error
    return ["--params-json", payload]


class SubprocessDispatcher:
    """Resolve each branch in a separate OS process via ``adag resolve-branch``.

    Workers share results through ``cache_dir``, so it must be set. Scale is
    limited to the local machine's cores.
    """

    def __init__(self, policy: BranchPolicy | None = None) -> None:
        self.policy = policy or BranchPolicy()

    def dispatch(self, branch: BranchRef, sub: StepResult[Any], context: ResolveContext) -> Any:
        cache_dir = context.cache_dir
        if cache_dir is None:
            raise ValueError("SubprocessDispatcher requires a cache_dir (the shared rendezvous).")

        if sub.is_cached(cache_dir, params=context.params):
            context.observer.on_event(
                "branch.cache_hit", {"key": branch.key, "hash": branch.branch_hash}
            )
            return sub.load(cache_dir, params=context.params)

        command = [
            sys.executable,
            "-m",
            "adag",
            "resolve-branch",
            branch.pipeline_module,
            "--fan-out",
            branch.fan_out_var,
            "--key",
            branch.key,
            "--cache-dir",
            str(cache_dir),
            *_param_args(context.params),
            "--expect-hash",
            branch.branch_hash,
        ]
        attempts = self.policy.max_retries + 1
        detail = ""
        for attempt in range(attempts):
            context.observer.on_event(
                "branch.dispatch", {"key": branch.key, "attempt": attempt + 1, "of": attempts}
            )
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.policy.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                # subprocess.run RAISES on timeout rather than returning — without catching it,
                # a timed-out branch would skip the retry loop AND surface as a raw
                # TimeoutExpired instead of BranchExecutionError. Treat it as a failed (retryable)
                # attempt so timeouts honour max_retries and the error contract.
                detail = f"timed out after {self.policy.timeout_seconds}s"
                logger.warning(
                    "branch.timeout",
                    key=branch.key,
                    attempt=attempt + 1,
                    timeout_s=self.policy.timeout_seconds,
                )
            else:
                if completed.returncode == 0 and sub.is_cached(cache_dir, params=context.params):
                    return sub.load(cache_dir, params=context.params)
                detail = (
                    completed.stderr.strip()[-500:]
                    or f"exit code {completed.returncode}, no result in cache"
                )
                if completed.returncode == EXIT_BAD_BRANCH:
                    break  # a bad reference or hash mismatch fails the same way every time
            if attempt + 1 < attempts:
                time.sleep(self.policy.poll_interval_seconds)  # backoff between attempts
        raise BranchExecutionError(branch.key, detail)
