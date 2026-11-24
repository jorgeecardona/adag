"""``CommandBackend`` runs a ``JobBackend`` from command templates, with no shell.

``fake_scheduler.py`` is the CLI the templates call. The last test runs real fan-out branches
through ``RemoteDispatcher``, a ``CommandBackend`` and that CLI.
"""

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from adag import NullObserver, Pipeline
from adag.backends import CommandBackend
from adag.cli import load_pipeline_module
from adag.deferred import BranchRef
from adag.dispatch import BranchJob, BranchPolicy, JobState, RemoteDispatcher, Resources
from test_remote_dispatcher import PIPELINE_SOURCE, _values  # pyright: ignore[reportPrivateUsage]

SCHEDULER = str(Path(__file__).with_name("fake_scheduler.py"))
STATES = {
    "RUNNING": JobState.RUNNING,
    "COMPLETED": JobState.SUCCEEDED,
    "FAILED": JobState.FAILED,
    "CANCELLED": JobState.FAILED,
}


def _backend(tmp_path: Path, **extra: Any) -> CommandBackend:
    run = [sys.executable, SCHEDULER]
    return CommandBackend(
        submit_command=[*run, "submit", "{name}", "{command}"],
        status_command=[*run, "status", "{job_id}"],
        cancel_command=[*run, "cancel", "{job_id}"],
        states=STATES,
        env={"FAKE_SCHED_DIR": str(tmp_path)},
        **extra,
    )


def _job(key: str = "a", params: str = "{}") -> BranchJob:
    ref = BranchRef(
        pipeline_module="pkg.pipeline", fan_out_var="per_key", key=key, branch_hash="abc123def456"
    )
    return BranchJob(branch=ref, params_json=params, cache_dir="/cache")


def _wait(backend: CommandBackend, job_id: str) -> JobState:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        answer = backend.status([job_id])
        if job_id in answer and answer[job_id].state is not JobState.RUNNING:
            return answer[job_id].state
        time.sleep(0.05)
    raise AssertionError("the job did not end")


def test_submit_returns_the_last_line_and_status_follows_the_job(tmp_path: Path) -> None:
    backend = _backend(tmp_path, logs_command=[sys.executable, SCHEDULER, "logs", "{job_id}"])
    job_id = backend.submit(_job())
    assert job_id == "job-1"
    # The worker command is not installed here, so the fake scheduler's process fails.
    assert _wait(backend, job_id) is JobState.FAILED
    status = backend.status([job_id])[job_id]
    assert status.exit_code not in (None, 0)
    assert backend.logs(job_id) is not None


def test_a_hostile_key_stays_one_argument(tmp_path: Path) -> None:
    script = tmp_path / "echo_args.py"
    script.write_text("import json, sys; print(json.dumps(sys.argv[1:])); print('id-1')\n")
    backend = CommandBackend(
        submit_command=[sys.executable, str(script), "{command}", "{command_line}", "{name}"],
        status_command=[sys.executable, "-c", "print('RUNNING')"],
        cancel_command=[sys.executable, "-c", ""],
        states=STATES,
    )
    key = "a; touch /tmp/pwned $(id) `id` {job_id} {{x}}"
    captured: list[list[str]] = []
    original = backend._run  # pyright: ignore[reportPrivateUsage]

    def spy(argv: list[str]) -> Any:
        captured.append(argv)
        return original(argv)

    backend._run = spy  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
    assert backend.submit(_job(key=key)) == "id-1"
    argv = captured[0]
    assert key in argv  # the key is one whole argument of {command}
    assert argv.count(key) == 1
    assert not Path("/tmp/pwned").exists()


def test_unknown_placeholders_are_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown placeholders"):
        CommandBackend(
            submit_command=["x", "{nope}"],
            status_command=["x"],
            cancel_command=["x"],
            states=STATES,
        )
    with pytest.raises(ValueError, match="unknown placeholders"):
        CommandBackend(
            submit_command=["x"],
            status_command=["x", "{name}"],
            cancel_command=["x"],
            states=STATES,
        )


def test_find_needs_the_name_in_submit() -> None:
    with pytest.raises(ValueError, match="find_command needs"):
        CommandBackend(
            submit_command=["x"],
            status_command=["x"],
            cancel_command=["x"],
            states=STATES,
            find_command=["x", "{name}"],
        )


def test_resources_fill_the_placeholders(tmp_path: Path) -> None:
    script = tmp_path / "show.py"
    script.write_text("import sys; print('|'.join(sys.argv[1:]))\n")
    backend = CommandBackend(
        submit_command=[sys.executable, str(script), "{cpus}", "{memory_mb}", "{gpus}"],
        status_command=["x"],
        cancel_command=["x"],
        states=STATES,
    )
    job = BranchJob(
        branch=_job().branch,
        params_json="{}",
        cache_dir=None,
        resources=Resources(cpus=2.5, memory_mb=4096, gpus=1),
    )
    assert backend.submit(job) == "2.5|4096|1"
    assert backend.submit(_job()) == "||0"


def test_submit_that_fails_or_prints_nothing_raises(tmp_path: Path) -> None:
    for code in ("raise SystemExit(3)", "pass"):
        backend = CommandBackend(
            submit_command=[sys.executable, "-c", code],
            status_command=["x"],
            cancel_command=["x"],
            states=STATES,
        )
        with pytest.raises(RuntimeError, match="submit command failed"):
            backend.submit(_job())


def test_a_bad_status_leaves_the_job_out_of_the_answer() -> None:
    cases = {
        "an unknown word": "print('WEIRD')",
        "a failing command": "raise SystemExit(1)",
        "nothing printed": "pass",
    }
    for code in cases.values():
        backend = CommandBackend(
            submit_command=["x"],
            status_command=[sys.executable, "-c", code],
            cancel_command=["x"],
            states=STATES,
        )
        assert backend.status(["j"]) == {}


def test_a_status_command_that_hangs_is_cut_off() -> None:
    backend = CommandBackend(
        submit_command=["x"],
        status_command=[sys.executable, "-c", "import time; time.sleep(30)"],
        cancel_command=["x"],
        states=STATES,
        command_timeout_seconds=0.3,
    )
    started = time.monotonic()
    assert backend.status(["j"]) == {}
    assert time.monotonic() - started < 10


def test_status_reads_exit_code_and_detail() -> None:
    backend = CommandBackend(
        submit_command=["x"],
        status_command=[sys.executable, "-c", "print('FAILED 137 out of memory')"],
        cancel_command=["x"],
        states=STATES,
    )
    status = backend.status(["j"])["j"]
    assert (status.state, status.exit_code, status.detail) == (
        JobState.FAILED,
        137,
        "out of memory",
    )


def test_cancel_never_raises() -> None:
    backend = CommandBackend(
        submit_command=["x"],
        status_command=["x"],
        cancel_command=["/no/such/program", "{job_id}"],
        states=STATES,
    )
    backend.cancel(["j1", "j2"])


def test_close_runs_once_after_a_submit(tmp_path: Path) -> None:
    mark = tmp_path / "closed.txt"
    backend = CommandBackend(
        submit_command=[sys.executable, "-c", "print('id')"],
        status_command=["x"],
        cancel_command=["x"],
        states=STATES,
        close_command=[
            sys.executable,
            "-c",
            f"open({str(mark)!r}, 'a').write('x')",
        ],
    )
    backend.close()  # nothing submitted yet
    assert not mark.exists()
    backend.submit(_job())
    backend.close()
    backend.close()
    assert mark.read_text() == "x"
    backend.submit(_job())  # works again after a close
    backend.close()
    assert mark.read_text() == "xx"


def test_find_returns_the_id_or_none(tmp_path: Path) -> None:
    backend = _backend(tmp_path, find_command=[sys.executable, SCHEDULER, "find", "{name}"])
    assert backend.find("adag-abc123def456-1") is None
    job_id = backend.submit(_job())
    assert backend.find(_job().name()) == job_id
    plain = _backend(tmp_path)
    assert plain.find("anything") is None
    assert plain.logs("job-1") is None


def test_runs_real_branches_through_the_dispatcher(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.py"
    path.write_text(PIPELINE_SOURCE)
    module = load_pipeline_module(path)
    pipeline = Pipeline(module.per_key, cache_dir=tmp_path / "cache")
    backend = _backend(
        tmp_path / "sched",
        find_command=[sys.executable, SCHEDULER, "find", "{name}"],
        logs_command=[sys.executable, SCHEDULER, "logs", "{job_id}"],
    )
    (tmp_path / "sched").mkdir()
    dispatcher = RemoteDispatcher(backend, BranchPolicy(poll_interval_seconds=0.1))
    result = pipeline.execute(dispatcher=dispatcher, observer=NullObserver())
    assert _values(result) == {"a": 2, "b": 4, "c": 6}
