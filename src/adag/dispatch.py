"""Branch dispatchers: resolve fan-out branches on workers, rejoin through the cache.

A ``BranchDispatcher`` (see ``deferred.py``) decides where the branches of a ``FanOutStep``
run. The default ``InlineDispatcher`` runs them in-process.

``RemoteDispatcher`` runs each branch as a job on a ``JobBackend``. The worker runs
``adag resolve-branch``, which re-imports the pipeline, re-derives the branch, resolves it and
writes the result to the shared cache under the branch content hash. The dispatcher then loads
the result by that hash. Only the ``BranchJob`` crosses to the worker.

``SubprocessDispatcher`` is ``RemoteDispatcher`` with ``LocalJobBackend``: one local process
per branch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import IO, Any, Protocol

import structlog

from adag._worker_events import WorkerRecord, events_key
from adag.cache_store import CacheStore
from adag.deferred import BranchRef, ResolveContext, send_event
from adag.step import StepResult

logger = structlog.get_logger()


class BranchExecutionError(RuntimeError):
    """Deprecated: adag no longer raises this. Branch failures arrive as ``FanOutBranchError``.

    Kept so ``except BranchExecutionError`` and custom dispatchers that raise it still import.
    Removed in 1.0.
    """

    def __init__(self, key: str, detail: str) -> None:
        self.key = key
        super().__init__(f"branch {key!r} failed: {detail}")


@dataclass(frozen=True)
class BranchPolicy:
    """How hard to try a branch and how patiently to wait for it.

    ``timeout_seconds`` counts from when the job is RUNNING. ``queue_timeout_seconds`` counts
    from submit while the job is PENDING; a batch queue can hold a job for hours.
    ``result_grace_seconds`` is how long to wait for the result to appear after a job
    SUCCEEDED, because a network filesystem can show a new file late.
    ``missing_status_polls`` is how many polls in a row may leave a job out of the backend's
    ``status()`` answer before the job counts as FAILED, so the retry policy applies.
    ``silent_timeout_seconds`` fails a RUNNING job whose worker wrote a sign of life and then went
    quiet for that long: a machine that died without the backend noticing. The worker writes at
    least every 10 seconds, so set it to 30 or more. It does not count the time before the first
    sign, so a slow start is covered by ``timeout_seconds``.
    """

    max_retries: int = 0
    timeout_seconds: float | None = None
    poll_interval_seconds: float = 5.0
    retry_delay_seconds: float = 0.0
    result_grace_seconds: float = 5.0
    # Added in 0.2.11. Kept after the first-release fields so BranchPolicy(1, 60, 2.0) still means
    # (max_retries, timeout_seconds, poll_interval_seconds).
    queue_timeout_seconds: float | None = None
    missing_status_polls: int = 3
    # Added in 0.3.44.
    silent_timeout_seconds: float | None = None


LOG_TAIL_CHARS = 2000
"""How much of a failed job's output goes into the ``FanOutBranchError`` detail."""

EXIT_BAD_BRANCH = 2
"""Worker exit code for a bad branch reference or a hash mismatch. Never retried."""


class BranchReferenceError(LookupError):
    """The worker cannot find the branch, or derives a different hash than the main process."""


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
    ``cache_dir``, or the store at ``store_url``. Success means the result is in the cache
    under ``branch.branch_hash``. The job's exit code alone is not trusted.
    """

    branch: BranchRef
    params_json: str
    cache_dir: str | None
    attempt: int = 1
    resources: Resources = Resources()
    store_url: str | None = None

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
            *(("--cache-dir", self.cache_dir) if self.cache_dir is not None else ()),
            *(("--store-url", self.store_url) if self.store_url is not None else ()),
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

    A backend may also have ``find``, ``logs`` and ``close`` (see ``SupportsFind``,
    ``SupportsLogs`` and ``SupportsClose``). The dispatcher uses each one that exists.
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


class SupportsFind(Protocol):
    """Optional ``JobBackend`` method: look a job up by name, so a rerun can reattach to it.

    ``RemoteDispatcher`` finds this by name (``getattr``), so a backend that has only
    ``submit``, ``status`` and ``cancel`` keeps working. A backend that has ``find`` must submit
    each job under ``BranchJob.name()``.
    """

    def find(self, name: str) -> str | None:
        """Return the id of the job submitted under ``name``, or ``None`` if the backend has none.

        A job that has ended may still be returned. The dispatcher asks ``status`` and reattaches
        only to a job that is PENDING or RUNNING.
        """
        ...


class SupportsLogs(Protocol):
    """Optional ``JobBackend`` method: the output of a job, shown when its branch fails."""

    def logs(self, job_id: str) -> str | None:
        """Return the job's output (the end is enough), or ``None`` when there is none."""
        ...


class SupportsClose(Protocol):
    """Optional ``JobBackend`` method: release what the backend created for the run."""

    def close(self) -> None:
        """Stop servers or connections the backend started. Called once per ``dispatch_all``.

        It must be safe to call twice, and the backend must work again if a later
        ``dispatch_all`` submits more jobs.
        """
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


@dataclass
class _LiveJob:
    """Mutable bookkeeping for one submitted job, private to ``RemoteDispatcher``."""

    ref: BranchRef
    sub: StepResult[Any]
    attempt: int
    submitted_at: float
    running_since: float | None = None
    succeeded_at: float | None = None
    missing_polls: int = 0
    # What workers wrote (``adag._worker_events``). A worker process has a token. ``seen_seq`` is
    # the last event forwarded per token, ``last_pair`` the last (token, write counter) seen and
    # ``last_sign_at`` when this process saw it change. Two workers can write one record (a
    # leftover of an earlier attempt), so no token is pinned.
    seen_seq: dict[str, int] = field(default_factory=dict[str, int])
    last_pair: tuple[str, int] | None = None
    last_sign_at: float | None = None
    worker_error: str = ""


@dataclass(frozen=True)
class _Queued:
    ref: BranchRef
    sub: StepResult[Any]
    attempt: int
    ready_at: float


class RemoteDispatcher:
    """Run each fan-out branch as a job on ``backend`` and load its result from the cache.

    The dispatcher skips branches already in the cache, submits the rest (at most
    ``max_concurrent`` at a time), polls ``backend.status`` once per
    ``policy.poll_interval_seconds`` for all live jobs, retries failed attempts up to
    ``policy.max_retries``, and cancels every live job when it stops early (an error, a
    timeout, or Ctrl-C). A job that exits with ``EXIT_BAD_BRANCH`` is not retried.

    The backend may have the optional methods ``find``, ``logs`` and ``close``:
    ``find`` lets a rerun reattach to a job that an earlier run left alive instead of submitting
    a duplicate, ``logs`` adds the end of a failed job's output to the error, and ``close`` is
    called once when ``dispatch_all`` ends. A branch's own ``resources`` (set on its
    ``FanOutStep``) replace the dispatcher's ``resources`` for that branch.

    When the pipeline has a ``store``, pass ``store_url`` (see ``store_from_url``): a store
    object cannot cross to a worker, so the worker opens the same store from the URL.

    ``clock`` and ``sleep`` are injectable so tests run without waiting.
    """

    def __init__(
        self,
        backend: JobBackend,
        policy: BranchPolicy | None = None,
        resources: Resources | None = None,
        max_concurrent: int | None = None,
        store_url: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.backend = backend
        self.policy = policy or BranchPolicy()
        self.resources = resources or Resources()
        self.max_concurrent = max_concurrent
        self.store_url = store_url
        self._clock = clock
        self._sleep = sleep

    def dispatch(self, branch: BranchRef, sub: StepResult[Any], context: ResolveContext) -> Any:
        """Resolve one branch. Kept from the pre-0.2.11 one-branch protocol; calls ``dispatch_all``."""
        return self.dispatch_all([(branch, sub)], context)[branch.key]

    def dispatch_all(
        self, branches: Sequence[tuple[BranchRef, StepResult[Any]]], context: ResolveContext
    ) -> dict[str, Any]:
        """Resolve every branch on the backend; return ``{key: result}``.

        Raises:
            ValueError: the run has neither ``cache_dir`` nor ``store``, or it has a ``store``
                and ``store_url`` was not given.
            TypeError: a param is not JSON-serializable.
            FanOutBranchError: one or more branches failed. Results of the others are in
                the cache, so a rerun submits only the failed keys.
        """
        store = context.result_store()
        if store is None:
            raise ValueError(
                f"{type(self).__name__} requires a cache_dir or store (the shared rendezvous)."
            )
        if context.store is not None and self.store_url is None:
            raise ValueError(
                f"{type(self).__name__} needs store_url= when the pipeline has a store, so "
                "workers can open the same store."
            )
        try:
            return self._dispatch(branches, context, store)
        finally:
            self._close_backend()

    def _close_backend(self) -> None:
        """Call the backend's optional ``close``. A failing ``close`` is logged, not raised."""
        close = getattr(self.backend, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception as error:
            logger.warning(
                "backend.close_failed", error_type=type(error).__name__, error=str(error)
            )

    def _dispatch(
        self,
        branches: Sequence[tuple[BranchRef, StepResult[Any]]],
        context: ResolveContext,
        store: CacheStore,
    ) -> dict[str, Any]:
        """Run ``dispatch_all`` after its checks: cache hits, submits, polls, retries."""
        from adag.fan_out import FanOutBranchError

        cache_dir = str(context.cache_dir) if context.store is None else None
        payload = params_json(context.params)

        results: dict[str, Any] = {}
        failed: dict[str, str] = {}
        now = self._clock()
        queue: deque[_Queued] = deque()
        for ref, sub in branches:
            loaded = sub.try_load(store, params=context.params)
            if loaded is not None:
                send_event(
                    context.observer, "branch.cache_hit", {"key": ref.key, "hash": ref.branch_hash}
                )
                results[ref.key] = loaded
            else:
                queue.append(_Queued(ref, sub, 1, now))

        live: dict[str, _LiveJob] = {}

        def fail(job_id: str, job: _LiveJob, detail: str, exit_code: int | None) -> None:
            del live[job_id]
            # The result may be in the cache already: an old job from a crashed run finished, or
            # this job wrote it and then failed. A result that appears after this check is too late.
            loaded = job.sub.try_load(store, params=context.params)
            if loaded is not None:
                results[job.ref.key] = loaded
                self._forget_events(store, job.ref)
                return
            self._read_events(job, store, context, self._clock())
            retryable = exit_code != EXIT_BAD_BRANCH and job.attempt <= self.policy.max_retries
            if retryable:
                ready_at = self._clock() + self.policy.retry_delay_seconds
                queue.append(_Queued(job.ref, job.sub, job.attempt + 1, ready_at))
                return
            if job.worker_error and job.worker_error not in detail:
                detail = f"{detail}\nworker error: {job.worker_error}"
            failed[job.ref.key] = self._with_log_tail(job_id, detail)
            context.observer.on_fan_out_branch_failed(job.ref.key, detail)

        try:
            while queue or live:
                self._submit_ready(queue, live, payload, cache_dir, context, store)
                if live:
                    statuses = self.backend.status(list(live))
                    now = self._clock()
                    for job_id, job in list(live.items()):
                        self._advance(
                            job_id,
                            job,
                            statuses.get(job_id),
                            now,
                            store,
                            live,
                            results,
                            fail,
                            context,
                        )
                if queue or live:
                    self._sleep(self.policy.poll_interval_seconds)
        finally:
            if live:
                self.backend.cancel(list(live))

        if failed:
            raise FanOutBranchError(succeeded=sorted(results), failed=failed)
        return results

    def _read_events(
        self, job: _LiveJob, store: CacheStore, context: ResolveContext, now: float
    ) -> None:
        """Read the worker's record, forward its new events and note a sign of life.

        An event ``e`` of the worker goes to the observer as ``branch.<e>`` with the branch ``key``
        and the ``attempt``. A record of another worker process (a leftover) is ignored. A store
        that fails is logged, and the branch goes on without events.
        """
        try:
            text = store.get(events_key(job.ref.branch_hash))
        except Exception as error:
            logger.warning(
                "worker.events_read_failed", error_type=type(error).__name__, error=str(error)
            )
            return
        record = WorkerRecord.from_json(text) if text is not None else None
        if record is None:
            return
        if (record.token, record.beat) != job.last_pair:
            job.last_pair = (record.token, record.beat)
            job.last_sign_at = now
        seen = job.seen_seq.get(record.token, 0)
        for event in record.events:
            if event.seq <= seen:
                continue
            job.seen_seq[record.token] = event.seq
            if event.event == "failed":
                error_type = event.fields.get("error_type", "")
                job.worker_error = f"{error_type}: {event.fields.get('error', '')}"
            send_event(
                context.observer,
                f"branch.{event.event}",
                {"key": job.ref.key, "attempt": job.attempt, **event.fields},
            )

    def _forget_events(self, store: CacheStore, ref: BranchRef) -> None:
        """Delete the worker's record of a branch. A store that fails is logged and skipped."""
        try:
            store.delete(events_key(ref.branch_hash))
        except Exception as error:
            logger.warning(
                "worker.events_delete_failed", error_type=type(error).__name__, error=str(error)
            )

    def _with_log_tail(self, job_id: str, detail: str) -> str:
        """Add the end of the job's output to ``detail`` when the backend has ``logs``."""
        logs = getattr(self.backend, "logs", None)
        if logs is None:
            return detail
        try:
            text = logs(job_id)
        except Exception as error:
            logger.warning("backend.logs_failed", error_type=type(error).__name__, error=str(error))
            return detail
        tail = (text or "").strip()[-LOG_TAIL_CHARS:]
        if not tail or tail in detail:
            return detail
        if tail.endswith(detail.strip()):
            return tail  # the detail is the end of the output: show the longer text once
        return f"{detail}\nlast output of job {job_id}:\n{tail}"

    def _reattach(
        self, item: _Queued, payload: str, cache_dir: str | None, context: ResolveContext
    ) -> tuple[str, int] | None:
        """Find a job of an earlier run for this branch that is still PENDING or RUNNING.

        A job name holds the attempt number, so this asks for every attempt the policy allows and
        takes the latest one that is alive. Returns ``(job_id, attempt)``, or ``None`` when the
        backend has no ``find``, knows no live job, or fails: the caller then submits a new job.
        """
        find = getattr(self.backend, "find", None)
        if find is None:
            return None
        known: dict[str, int] = {}
        try:
            for attempt in range(1, self.policy.max_retries + 2):
                name = BranchJob(item.ref, payload, cache_dir, attempt).name()
                job_id = find(name)
                if job_id is not None:
                    known[job_id] = attempt
            if not known:
                return None
            statuses = self.backend.status(list(known))
        except Exception as error:
            logger.warning("backend.find_failed", error_type=type(error).__name__, error=str(error))
            return None
        alive = [
            (attempt, job_id)
            for job_id, attempt in known.items()
            if (status := statuses.get(job_id)) is not None
            and status.state in (JobState.PENDING, JobState.RUNNING)
        ]
        if not alive:
            return None
        attempt, job_id = max(alive)
        send_event(
            context.observer,
            "branch.reattach",
            {"key": item.ref.key, "attempt": attempt, "job_id": job_id},
        )
        return job_id, attempt

    def _submit_ready(
        self,
        queue: deque[_Queued],
        live: dict[str, _LiveJob],
        payload: str,
        cache_dir: str | None,
        context: ResolveContext,
        store: CacheStore,
    ) -> None:
        """Submit queued attempts whose retry delay has passed, up to ``max_concurrent``."""
        now = self._clock()
        for _ in range(len(queue)):
            if self.max_concurrent is not None and len(live) >= self.max_concurrent:
                return
            item = queue.popleft()
            if item.ready_at > now:
                queue.append(item)
                continue
            if item.attempt == 1:
                found = self._reattach(item, payload, cache_dir, context)
                if found is not None:
                    live[found[0]] = _LiveJob(item.ref, item.sub, found[1], now)
                    continue
            resources = item.ref.resources if item.ref.resources is not None else self.resources
            self._forget_events(store, item.ref)
            job = BranchJob(item.ref, payload, cache_dir, item.attempt, resources, self.store_url)
            send_event(
                context.observer,
                "branch.dispatch",
                {"key": item.ref.key, "attempt": item.attempt, "of": self.policy.max_retries + 1},
            )
            job_id = self.backend.submit(job)
            live[job_id] = _LiveJob(item.ref, item.sub, item.attempt, now)

    def _advance(
        self,
        job_id: str,
        job: _LiveJob,
        status: JobStatus | None,
        now: float,
        store: CacheStore,
        live: dict[str, _LiveJob],
        results: dict[str, Any],
        fail: Callable[[str, _LiveJob, str, int | None], None],
        context: ResolveContext,
    ) -> None:
        """Move one live job forward from its latest status, or ``None`` when it is not reported.

        The cache comes first: a result already there ends the branch, whatever the job reports.
        """
        policy = self.policy
        loaded = job.sub.try_load(store, params=context.params)
        if loaded is not None:
            del live[job_id]
            results[job.ref.key] = loaded
            self._read_events(job, store, context, now)
            self._forget_events(store, job.ref)
            if status is None or status.state in (JobState.PENDING, JobState.RUNNING):
                self.backend.cancel([job_id])
            return
        if status is None:
            job.missing_polls += 1
            if job.missing_polls >= policy.missing_status_polls:
                self.backend.cancel([job_id])
                detail = f"the backend did not report job {job_id} in {job.missing_polls} polls"
                fail(job_id, job, detail, None)
            return
        job.missing_polls = 0
        self._read_events(job, store, context, now)
        if status.state is JobState.SUCCEEDED:
            if job.succeeded_at is None:
                job.succeeded_at = now
            if now - job.succeeded_at >= policy.result_grace_seconds:
                fail(job_id, job, "job succeeded but no result is in the cache", None)
            return
        if status.state is JobState.FAILED:
            fail(job_id, job, status.detail or "job failed", status.exit_code)
            return
        if status.state is JobState.RUNNING:
            if job.running_since is None:
                job.running_since = now
            timeout = policy.timeout_seconds
            if timeout is not None and now - job.running_since > timeout:
                self.backend.cancel([job_id])
                logger.warning("branch.timeout", key=job.ref.key, attempt=job.attempt)
                fail(job_id, job, f"timed out after {timeout}s", None)
                return
            silent = policy.silent_timeout_seconds
            if (
                silent is not None
                and job.last_sign_at is not None
                and now - job.last_sign_at > silent
            ):
                self.backend.cancel([job_id])
                logger.warning("branch.silent", key=job.ref.key, attempt=job.attempt)
                fail(job_id, job, f"the worker wrote nothing for {silent}s", None)
            return
        queue_timeout = policy.queue_timeout_seconds
        if queue_timeout is not None and now - job.submitted_at > queue_timeout:
            self.backend.cancel([job_id])
            fail(job_id, job, f"still queued after {queue_timeout}s", None)


class LocalJobBackend:
    """Run each branch job as a local ``python -m adag resolve-branch`` process.

    stderr goes to a temporary file, so a chatty worker cannot block on a full pipe. Its last
    500 characters become the ``detail`` of a FAILED status. ``logs`` returns the last
    ``LOG_KEEP_CHARS`` characters of a failed job's stderr, until ``close``.

    It has no ``find``: the jobs live in this Python process, so a restarted run knows none of
    them. ``find`` matters for a scheduler that outlives the dispatcher.
    """

    LOG_KEEP_CHARS = 8000

    def __init__(self) -> None:
        self._procs: dict[str, tuple[subprocess.Popen[bytes], IO[bytes]]] = {}
        self._failed_logs: dict[str, str] = {}
        self._next = 0

    def submit(self, job: BranchJob) -> str:
        # Closed in status() once the process ends, or in cancel().
        stderr = tempfile.TemporaryFile()  # noqa: SIM115
        proc = subprocess.Popen(
            job.command(prefix=(sys.executable, "-m", "adag")),
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
        self._next += 1
        job_id = f"local-{self._next}"
        self._procs[job_id] = (proc, stderr)
        return job_id

    def status(self, job_ids: Sequence[str]) -> dict[str, JobStatus]:
        statuses: dict[str, JobStatus] = {}
        for job_id in job_ids:
            proc, stderr = self._procs[job_id]
            code = proc.poll()
            if code is None:
                statuses[job_id] = JobStatus(JobState.RUNNING)
                continue
            if code == 0:
                statuses[job_id] = JobStatus(JobState.SUCCEEDED, exit_code=0)
            else:
                stderr.seek(0)
                text = stderr.read().decode(errors="replace").strip()
                self._failed_logs[job_id] = text[-self.LOG_KEEP_CHARS :]
                statuses[job_id] = JobStatus(
                    JobState.FAILED, text[-500:] or f"exit code {code}", exit_code=code
                )
            stderr.close()
            del self._procs[job_id]
        return statuses

    def cancel(self, job_ids: Sequence[str]) -> None:
        for job_id in job_ids:
            entry = self._procs.pop(job_id, None)
            if entry is None:
                continue
            proc, stderr = entry
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            stderr.close()

    def logs(self, job_id: str) -> str | None:
        """The end of stderr of a job that failed, or ``None`` (running, succeeded, unknown)."""
        return self._failed_logs.get(job_id)

    def close(self) -> None:
        """Stop jobs that are still running and forget the saved logs."""
        self.cancel(list(self._procs))
        self._failed_logs.clear()


class SubprocessDispatcher(RemoteDispatcher):
    """Resolve each branch in a local OS process via ``adag resolve-branch``.

    ``RemoteDispatcher`` with ``LocalJobBackend``. Runs at most ``max_concurrent`` processes at
    once (default: the number of CPUs). Workers share results through ``cache_dir``, or the
    store at ``store_url``. The pipeline must have one of them.
    """

    def __init__(
        self,
        policy: BranchPolicy | None = None,
        max_concurrent: int | None = None,
        store_url: str | None = None,
    ) -> None:
        super().__init__(
            LocalJobBackend(),
            policy or BranchPolicy(poll_interval_seconds=0.05),
            max_concurrent=max_concurrent or os.cpu_count() or 1,
            store_url=store_url,
        )
