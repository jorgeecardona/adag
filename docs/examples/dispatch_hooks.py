"""A job backend with the optional methods: find, logs and close."""

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from adag import NullObserver, Pipeline
from adag.cli import load_pipeline_module
from adag.dispatch import (
    BranchJob,
    BranchPolicy,
    JobState,
    JobStatus,
    LocalJobBackend,
    RemoteDispatcher,
)
from adag.fan_out import FanOutBranchError


class RecordingBackend(LocalJobBackend):
    """LocalJobBackend already has logs and close. This adds find and records the calls.

    It also reports a failed job with only its exit code, as most schedulers do, so the output
    reaches the error through ``logs``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.ids_by_name: dict[str, str] = {}
        self.calls: list[str] = []

    def submit(self, job: BranchJob) -> str:
        self.calls.append("submit")
        job_id = super().submit(job)
        self.ids_by_name[job.name()] = job_id  # find() needs the name that submit() used
        return job_id

    def find(self, name: str) -> str | None:
        self.calls.append("find")
        return self.ids_by_name.get(name)

    def status(self, job_ids: Sequence[str]) -> Mapping[str, JobStatus]:
        reported: dict[str, JobStatus] = {}
        for job_id, status in super().status(job_ids).items():
            failed = status.state is JobState.FAILED
            detail = f"exit code {status.exit_code}" if failed else status.detail
            reported[job_id] = JobStatus(status.state, detail, status.exit_code)
        return reported

    def close(self) -> None:
        self.calls.append("close")
        super().close()

    def cancel(self, job_ids: Sequence[str]) -> None:
        self.calls.append("cancel")
        super().cancel(job_ids)


module = load_pipeline_module(Path(__file__).with_name("dispatch_failing_pipeline.py"))
pipeline = Pipeline(module.per_key, cache_dir=Path(tempfile.mkdtemp()))
backend = RecordingBackend()
dispatcher = RemoteDispatcher(backend, BranchPolicy(poll_interval_seconds=0.05), max_concurrent=3)

try:
    pipeline.execute(observer=NullObserver(), dispatcher=dispatcher)
except FanOutBranchError as error:
    print("succeeded:", error.succeeded)
    print("b:", error.failed["b"])

print({name: backend.calls.count(name) for name in ("find", "submit", "close")})
