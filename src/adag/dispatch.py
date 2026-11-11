"""Branch dispatchers — resolve fan-out branches on workers, rejoin via the cache.

A ``BranchDispatcher`` (see ``deferred.py``) decides *how* a ``FanOutStep`` branch
is resolved. The default ``InlineDispatcher`` runs it in-process. The dispatchers
here run each branch on a *worker* — a subprocess (dev/tests) or an AWS Batch job
(production) — that re-imports the pipeline, re-derives the branch, resolves it,
and writes the result to the shared cache keyed by the branch content hash. The
orchestrator then loads it back by that same hash: the cache is the wire, so
nothing but ``(module, fan_out_var, key, params, cache_dir)`` ever crosses it.

``SubprocessDispatcher`` is the local stand-in: same worker entry
(``adag resolve-branch``), same cache-as-IPC, zero AWS — used to test the
whole loop. ``BatchDispatcher`` swaps the subprocess spawn for a Batch job and
contains all async at its own boundary (``asyncio.run``), never leaking awaitables
into the synchronous resolve path.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from adag.deferred import BranchRef, ResolveContext
from adag.step import StepResult

logger = structlog.get_logger()

# Environment-variable keys a Batch worker reads to resolve one branch. Shared
# between BatchDispatcher (which sets them) and the Batch container entry point.
ENV_BRANCH_MODULE = "ADAG_BRANCH_MODULE"
ENV_BRANCH_FAN_OUT = "ADAG_BRANCH_FAN_OUT"
ENV_BRANCH_KEY = "ADAG_BRANCH_KEY"
ENV_BRANCH_CACHE_DIR = "ADAG_BRANCH_CACHE_DIR"
ENV_BRANCH_PARAMS = "ADAG_BRANCH_PARAMS"  # JSON object of pipeline params


@dataclass(frozen=True)
class BranchPolicy:
    """How hard to try a branch and how patiently to wait for it."""

    max_retries: int = 0
    timeout_seconds: float | None = None
    poll_interval_seconds: float = 5.0


class BranchExecutionError(RuntimeError):
    """A single branch's worker failed (nonzero exit, timeout, or job FAILED)."""

    def __init__(self, key: str, detail: str) -> None:
        self.key = key
        super().__init__(f"branch {key!r} failed: {detail}")


def _param_args(params: dict[str, Any]) -> list[str]:
    """Render run params as repeated ``--param k=v`` CLI args for the worker."""
    args: list[str] = []
    for name, value in params.items():
        args.extend(["--param", f"{name}={value}"])
    return args


def _batch_job_name(branch: BranchRef) -> str:
    """A valid AWS Batch job name: <=128 chars of [A-Za-z0-9_-]. The content hash LEADS so it
    survives the 128-char truncation — it is what makes the name unique per branch — while the
    fan-out var and key (sanitized; keys can hold '/' or ':' that Batch rejects) are descriptive
    context that may be truncated away.
    """
    safe_var = re.sub(r"[^A-Za-z0-9_-]", "-", branch.fan_out_var)
    safe_key = re.sub(r"[^A-Za-z0-9_-]", "-", branch.key)
    return f"branch-{branch.branch_hash}-{safe_var}-{safe_key}"[:128]


class SubprocessDispatcher:
    """Resolve each branch in a separate OS process via ``adag resolve-branch``.

    The local stand-in for a remote dispatcher: identical worker entry and
    cache-as-IPC, no AWS. Suitable for development and tests; not for production
    distribution (no horizontal scale beyond local cores).
    """

    def __init__(self, policy: BranchPolicy | None = None) -> None:
        self.policy = policy or BranchPolicy()

    def dispatch(self, branch: BranchRef, sub: StepResult[Any], context: ResolveContext) -> Any:
        cache_dir = context.cache_dir
        if cache_dir is None:
            raise ValueError("SubprocessDispatcher requires a cache_dir (the shared rendezvous).")

        if sub.is_cached(cache_dir, params=context.params):
            logger.info("branch.cache_hit", key=branch.key, hash=branch.branch_hash)
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
        ]
        attempts = self.policy.max_retries + 1
        detail = ""
        for attempt in range(attempts):
            logger.info("branch.dispatch", key=branch.key, attempt=attempt + 1, of=attempts)
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
            if attempt + 1 < attempts:
                time.sleep(self.policy.poll_interval_seconds)  # backoff between attempts
        raise BranchExecutionError(branch.key, detail)


class BatchJobStatus(Protocol):
    """The terminal-state fields BatchDispatcher reads from a job description."""

    status: str
    exit_code: int | None


class BatchJobClient(Protocol):
    """The subset of an AWS Batch client BatchDispatcher needs.

    Structural so adag does not depend on any AWS SDK: the caller injects a client
    that conforms.
    """

    async def submit_job(
        self, *, job_name: str, environment: dict[str, str], vcpus: int, memory_mb: int, gpu: bool
    ) -> str: ...

    async def describe_job(self, job_id: str) -> BatchJobStatus: ...


_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED"})


class BatchDispatcher:
    """Resolve each branch as an AWS Batch job, rejoining through the shared (EFS)
    cache by content hash.

    The branch's scope is passed as environment variables (the Batch container
    entry reads them and runs ``resolve-branch``). All async is contained here:
    ``dispatch`` is synchronous and bridges via ``asyncio.run`` so awaitables
    never reach the DAG's synchronous resolve path.
    """

    def __init__(
        self,
        batch_client: BatchJobClient,
        *,
        vcpus: int = 8,
        memory_mb: int = 65536,
        gpu: bool = True,
        policy: BranchPolicy | None = None,
    ) -> None:
        self.batch_client = batch_client
        self.vcpus = vcpus
        self.memory_mb = memory_mb
        self.gpu = gpu
        # Distributed jobs get one retry by default: a Batch job lost to a spot reclaim or a
        # transient infra blip should re-run rather than fail the whole fan-out.
        self.policy = policy or BranchPolicy(max_retries=1, poll_interval_seconds=30.0)

    def dispatch(self, branch: BranchRef, sub: StepResult[Any], context: ResolveContext) -> Any:
        cache_dir = context.cache_dir
        if cache_dir is None:
            raise ValueError("BatchDispatcher requires a cache_dir (the shared EFS rendezvous).")

        if sub.is_cached(cache_dir, params=context.params):
            logger.info("branch.cache_hit", key=branch.key, hash=branch.branch_hash)
            return sub.load(cache_dir, params=context.params)

        environment = {
            ENV_BRANCH_MODULE: branch.pipeline_module,
            ENV_BRANCH_FAN_OUT: branch.fan_out_var,
            ENV_BRANCH_KEY: branch.key,
            ENV_BRANCH_CACHE_DIR: str(cache_dir),
            ENV_BRANCH_PARAMS: json.dumps(context.params),
        }
        attempts = self.policy.max_retries + 1
        detail = ""
        for attempt in range(attempts):
            try:
                asyncio.run(self._submit_and_wait(branch, environment))
            except BranchExecutionError as exc:
                detail = str(exc)
                logger.warning(
                    "branch.batch_attempt_failed",
                    key=branch.key,
                    attempt=attempt + 1,
                    of=attempts,
                    error=detail,
                )
            else:
                if sub.is_cached(cache_dir, params=context.params):
                    return sub.load(cache_dir, params=context.params)
                detail = "job finished but no result landed in the shared cache (hash mismatch?)"
            if attempt + 1 < attempts:
                time.sleep(self.policy.poll_interval_seconds)  # backoff before resubmit
        raise BranchExecutionError(branch.key, detail)

    async def _submit_and_wait(self, branch: BranchRef, environment: dict[str, str]) -> None:
        job_id = await self.batch_client.submit_job(
            job_name=_batch_job_name(branch),
            environment=environment,
            vcpus=self.vcpus,
            memory_mb=self.memory_mb,
            gpu=self.gpu,
        )
        logger.info("branch.batch_submitted", key=branch.key, job_id=job_id)
        deadline = (
            None
            if self.policy.timeout_seconds is None
            else time.monotonic() + self.policy.timeout_seconds
        )
        while True:
            status = await self.batch_client.describe_job(job_id)
            if status.status in _TERMINAL_STATUSES:
                break
            if deadline is not None and time.monotonic() > deadline:
                raise BranchExecutionError(
                    branch.key, f"timed out after {self.policy.timeout_seconds}s (job {job_id})"
                )
            await asyncio.sleep(self.policy.poll_interval_seconds)

        if status.status == "FAILED" or (status.exit_code is not None and status.exit_code != 0):
            raise BranchExecutionError(
                branch.key, f"Batch job {job_id} {status.status} (exit {status.exit_code})"
            )
