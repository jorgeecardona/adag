"""``CommandBackend``: a ``JobBackend`` built from command templates, for any scheduler with a CLI.

Give it the command that submits a job, the command that reports its state, and the command that
cancels it. It runs each one with ``subprocess`` and no shell, so a branch key or a parameter
never reaches a shell parser. It works with ``sbatch``, ``kubectl``, ``gcloud``, ``vastai``,
``modal``, ``ssh`` or a script of your own.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence

import structlog

from adag.dispatch import BranchJob, JobState, JobStatus

logger = structlog.get_logger()

_TOKEN = re.compile(r"\{\{|\}\}|\{(\w+)\}")
_SUBMIT_NAMES = frozenset({"command", "command_line", "name", "cpus", "memory_mb", "gpus"})
_JOB_NAMES = frozenset({"job_id"})
_FIND_NAMES = frozenset({"name"})
_OUTPUT_KEEP = 500


def _placeholders(template: Sequence[str]) -> set[str]:
    return {m.group(1) for element in template for m in _TOKEN.finditer(element) if m.group(1)}


def _expand(element: str, values: Mapping[str, str]) -> str:
    """Replace each ``{name}`` once. A value is never read again, so it cannot add a placeholder."""

    def one(match: re.Match[str]) -> str:
        piece = match.group(0)
        if piece == "{{":
            return "{"
        if piece == "}}":
            return "}"
        return values[match.group(1)]

    return _TOKEN.sub(one, element)


class CommandBackend:
    """Run jobs with command templates. Each template is a list of arguments, never a shell string.

    Placeholders, written ``{name}`` (write ``{{`` for a literal brace):

    - ``submit_command``: ``{command}`` (as a whole argument: it becomes the worker's arguments),
      ``{command_line}`` (the worker command as one shell-quoted text, for ``ssh`` or
      ``sbatch --wrap``), ``{name}``, ``{cpus}``, ``{memory_mb}``, ``{gpus}``. A resource that is
      not set becomes an empty text.
    - ``status_command``, ``cancel_command``, ``logs_command``: ``{job_id}``.
    - ``find_command``: ``{name}``.

    The commands must do this:

    - ``submit_command`` exits 0 and prints the job id as the last non-empty line.
    - ``status_command`` exits 0 and prints the state as its first word. The word must be a key
      of ``states``. An optional second word is the exit code, and the rest is the ``detail``.
    - ``cancel_command`` may fail. The job may have ended already.
    - ``find_command`` prints the id of the job submitted under ``{name}``, or nothing.
    - ``logs_command`` prints the output of the job.
    - ``close_command`` runs once after a ``submit``, when the dispatcher closes the backend.

    A status call that fails, times out, or prints a word that is not in ``states`` leaves the job
    out of the answer. ``BranchPolicy.missing_status_polls`` then decides when the job counts as
    FAILED, so one slow call does not fail a branch.

    Put secrets in ``env``, not in a template: a template shows in ``ps`` and in scheduler
    consoles. The commands run with the environment of this process plus ``env``. A POSIX shell
    is assumed wherever ``{command_line}`` is used.

    Raises:
        ValueError: a template has an unknown placeholder, or ``find_command`` is set and
            ``submit_command`` has no ``{name}``.
    """

    def __init__(
        self,
        *,
        submit_command: Sequence[str],
        status_command: Sequence[str],
        cancel_command: Sequence[str],
        states: Mapping[str, JobState],
        logs_command: Sequence[str] | None = None,
        find_command: Sequence[str] | None = None,
        close_command: Sequence[str] | None = None,
        command_timeout_seconds: float = 60.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._submit = tuple(submit_command)
        self._status = tuple(status_command)
        self._cancel = tuple(cancel_command)
        self._logs = tuple(logs_command) if logs_command is not None else None
        self._find = tuple(find_command) if find_command is not None else None
        self._close = tuple(close_command) if close_command is not None else None
        self._states = dict(states)
        self._timeout = command_timeout_seconds
        self._env = dict(env) if env is not None else None
        self._submitted = False
        for label, template, allowed in (
            ("submit_command", self._submit, _SUBMIT_NAMES),
            ("status_command", self._status, _JOB_NAMES),
            ("cancel_command", self._cancel, _JOB_NAMES),
            ("logs_command", self._logs, _JOB_NAMES),
            ("find_command", self._find, _FIND_NAMES),
            ("close_command", self._close, frozenset[str]()),
        ):
            if template is None:
                continue
            if not template:
                raise ValueError(f"{label} is empty")
            unknown = _placeholders(template) - allowed
            if unknown:
                raise ValueError(
                    f"{label} has unknown placeholders {sorted(unknown)}; allowed: {sorted(allowed)}"
                )
        if self._find is not None and "name" not in _placeholders(self._submit):
            raise ValueError("find_command needs {name} in submit_command, so find can see the job")

    def submit(self, job: BranchJob) -> str:
        values = {
            "command_line": shlex.join(job.command()),
            "name": job.name(),
            "cpus": "" if job.resources.cpus is None else f"{job.resources.cpus:g}",
            "memory_mb": "" if job.resources.memory_mb is None else str(job.resources.memory_mb),
            "gpus": str(job.resources.gpus),
        }
        argv: list[str] = []
        for element in self._submit:
            if element == "{command}":
                argv.extend(job.command())
            else:
                argv.append(_expand(element, values))
        self._submitted = True
        result = self._run(argv)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0 or not lines:
            raise RuntimeError(
                f"submit command failed (exit {result.returncode}, "
                f"{'no job id on stdout' if not lines else 'job id given'}): "
                f"{self._tail(result)}"
            )
        return lines[-1]

    def status(self, job_ids: Sequence[str]) -> dict[str, JobStatus]:
        answer: dict[str, JobStatus] = {}
        for job_id in job_ids:
            try:
                result = self._run(self._expand(self._status, {"job_id": job_id}))
            except RuntimeError as error:
                logger.warning("command_backend.status_failed", job_id=job_id, error=str(error))
                continue
            words = result.stdout.split(None, 2)
            if result.returncode != 0 or not words or words[0] not in self._states:
                logger.warning(
                    "command_backend.status_unknown",
                    job_id=job_id,
                    exit_code=result.returncode,
                    output=self._tail(result),
                )
                continue
            exit_code: int | None = None
            detail = ""
            if len(words) > 1:
                try:
                    exit_code = int(words[1])
                except ValueError:
                    detail = " ".join(words[1:])
                else:
                    detail = " ".join(words[2:]).strip()
            answer[job_id] = JobStatus(self._states[words[0]], detail, exit_code)
        return answer

    def cancel(self, job_ids: Sequence[str]) -> None:
        for job_id in job_ids:
            try:
                result = self._run(self._expand(self._cancel, {"job_id": job_id}))
            except RuntimeError as error:
                logger.warning("command_backend.cancel_failed", job_id=job_id, error=str(error))
                continue
            if result.returncode != 0:
                logger.info("command_backend.cancel_nonzero", job_id=job_id)

    def find(self, name: str) -> str | None:
        """Return the id the ``find_command`` prints, or ``None``. Always ``None`` without one."""
        if self._find is None:
            return None
        result = self._run(self._expand(self._find, {"name": name}))
        if result.returncode != 0:
            raise RuntimeError(
                f"find command failed (exit {result.returncode}): {self._tail(result)}"
            )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return lines[-1] if lines else None

    def logs(self, job_id: str) -> str | None:
        """Return what the ``logs_command`` prints, or ``None`` when it fails or prints nothing."""
        if self._logs is None:
            return None
        try:
            result = self._run(self._expand(self._logs, {"job_id": job_id}))
        except RuntimeError:
            return None
        text = result.stdout.strip()
        return text if result.returncode == 0 and text else None

    def close(self) -> None:
        """Run the ``close_command`` once if a job was submitted since the last close."""
        if self._close is None or not self._submitted:
            return
        self._submitted = False
        try:
            result = self._run(list(self._close))
        except RuntimeError as error:
            logger.warning("command_backend.close_failed", error=str(error))
            return
        if result.returncode != 0:
            logger.warning("command_backend.close_nonzero", exit_code=result.returncode)

    @staticmethod
    def _expand(template: Sequence[str], values: Mapping[str, str]) -> list[str]:
        return [_expand(element, values) for element in template]

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, **self._env} if self._env is not None else None
        try:
            return subprocess.run(  # noqa: S603  # an argument list, no shell, by design
                argv,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=self._timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"command timed out after {self._timeout:g} s") from error
        except OSError as error:
            raise RuntimeError(f"command could not start: {error}") from error

    @staticmethod
    def _tail(result: subprocess.CompletedProcess[str]) -> str:
        text = (result.stderr or result.stdout or "").strip()
        return text[-_OUTPUT_KEEP:]
