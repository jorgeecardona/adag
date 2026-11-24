"""A tiny scheduler CLI for ``test_command_backend``. It keeps one JSON file per job.

Usage (the state directory comes from ``FAKE_SCHED_DIR``):

- ``submit NAME ARGS...``: record a job and start ARGS in the background. Prints the job id.
- ``status ID``: print ``RUNNING``, ``COMPLETED 0`` or ``FAILED CODE``.
- ``cancel ID``: stop the job.
- ``find NAME``: print the id of the job with that name.
- ``logs ID``: print the output of the job.
- ``_run ID ARGS...``: internal. Runs ARGS and records how it ended.

A first argument ``adag`` is run as ``python -m adag``, so the tests need no installed script.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ["FAKE_SCHED_DIR"])


def _path(job_id: str) -> Path:
    return ROOT / f"{job_id}.json"


def _load(job_id: str) -> dict[str, object]:
    return json.loads(_path(job_id).read_text())


def main(argv: list[str]) -> int:
    command, rest = argv[0], argv[1:]
    if command == "submit":
        name, args = rest[0], rest[1:]
        job_id = f"job-{len(list(ROOT.glob('job-*.json'))) + 1}"
        _path(job_id).write_text(json.dumps({"name": name, "state": "RUNNING", "log": ""}))
        subprocess.Popen(
            [sys.executable, __file__, "_run", job_id, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print("submitted")
        print(job_id)
        return 0
    if command == "_run":
        job_id, args = rest[0], rest[1:]
        if args[0] == "adag":
            args = [sys.executable, "-m", "adag", *args[1:]]
        done = subprocess.run(args, capture_output=True, text=True, check=False)
        record = _load(job_id)
        if record["state"] == "CANCELLED":
            return 0
        record["state"] = "COMPLETED" if done.returncode == 0 else "FAILED"
        record["code"] = done.returncode
        record["log"] = (done.stderr or done.stdout)[-2000:]
        _path(job_id).write_text(json.dumps(record))
        return 0
    if command == "status":
        if not _path(rest[0]).exists():
            return 1
        record = _load(rest[0])
        print(" ".join(str(x) for x in (record["state"], record.get("code", ""), "") if x != ""))
        return 0
    if command == "cancel":
        record = _load(rest[0])
        if record["state"] == "RUNNING":
            record["state"] = "CANCELLED"
            _path(rest[0]).write_text(json.dumps(record))
        return 0
    if command == "find":
        for path in sorted(ROOT.glob("job-*.json")):
            if json.loads(path.read_text())["name"] == rest[0]:
                print(path.stem)
        return 0
    if command == "logs":
        print(_load(rest[0])["log"])
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
