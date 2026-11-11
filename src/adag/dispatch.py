"""Branch dispatchers — resolve fan-out branches on workers, rejoin via the cache.

A ``BranchDispatcher`` (see ``deferred.py``) decides *how* a ``FanOutStep`` branch
is resolved. The default ``InlineDispatcher`` runs it in-process.
``SubprocessDispatcher`` runs each branch in a worker process that re-imports the pipeline, re-derives the branch, resolves it,
and writes the result to the shared cache keyed by the branch content hash. The
orchestrator then loads it back by that same hash: the cache is the wire, so
nothing but ``(module, fan_out_var, key, params, cache_dir)`` ever crosses it.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

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
