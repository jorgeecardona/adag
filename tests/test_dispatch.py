"""Branch dispatch end to end: a worker resolves a branch, the orchestrator picks it up by hash.

The SubprocessDispatcher tests are the real proof of cache-as-IPC: branches resolve in
separate OS processes, write their results to a shared cache directory keyed by content
hash, and the orchestrator loads them back — no result ever crosses the wire, only the
branch reference.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

import adag.dispatch as dispatch_module
from adag.cli import load_pipeline_module, main, run_branch
from adag.dispatch import (
    EXIT_BAD_BRANCH,
    SubprocessDispatcher,
)
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
    # checked-out source so the worker runs the same code as this test.
    src_path = str(Path(__file__).resolve().parents[1] / "src")
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


def test_param_args_keep_value_types():
    """A str "5" stays a str and an int 5 stays an int across the worker boundary."""
    [flag, payload] = dispatch_module._param_args({"label": "5", "n": 5, "flag": True})
    assert flag == "--params-json"
    assert json.loads(payload) == {"label": "5", "n": 5, "flag": True}


def test_param_args_reject_values_a_worker_cannot_receive():
    with pytest.raises(TypeError, match="JSON-serializable"):
        dispatch_module._param_args({"p": object()})


def test_worker_exits_2_on_hash_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A worker that derives a different branch hash refuses to write a result."""
    path = _write_pipeline(tmp_path)
    code = main(
        [
            "resolve-branch",
            str(path),
            "--fan-out",
            "per_key",
            "--key",
            "a",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--expect-hash",
            "000000000000",
        ]
    )
    assert code == EXIT_BAD_BRANCH
    assert "hash mismatch" in capsys.readouterr().err
    assert not (tmp_path / "cache").exists() or not any((tmp_path / "cache").iterdir())


def test_worker_exits_2_on_unknown_key(tmp_path: Path):
    path = _write_pipeline(tmp_path)
    args = ["resolve-branch", str(path), "--fan-out", "per_key", "--key", "zzz"]
    assert main([*args, "--cache-dir", str(tmp_path / "cache")]) == EXIT_BAD_BRANCH


def test_worker_accepts_a_module_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A worker can import the pipeline by dotted module name instead of a file path."""
    package = tmp_path / "mypkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "pipe.py").write_text(PIPELINE_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))  # pyright: ignore[reportUnknownMemberType]

    module = load_pipeline_module("mypkg.pipe")
    fan: Any = module.per_key.step
    expected = fan.factory("b", fan.over["b"]).content_hash()

    result = run_branch("mypkg.pipe", "per_key", "b", {}, tmp_path / "cache", expect_hash=expected)
    assert result.value == 4


def test_missing_module_name_is_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_pipeline_module("no_such_pkg_xyz.pipe")


def test_fan_out_rejects_keys_with_the_same_text(tmp_path: Path):
    """Keys 1 and "1" would reach the same worker branch, so dispatch refuses them."""
    from dataclasses import dataclass

    from pydantic import BaseModel

    from adag import FanOutStep, Step

    class Out(BaseModel):
        n: int

    @dataclass(frozen=True)
    class Make(Step[Out]):
        n: int

        def run(self, n: int) -> Out:
            return Out(n=n)

    fan = FanOutStep(
        over={1: 1, "1": 2},
        factory=lambda key, value: Make(n=value)(),
        pipeline_module="x",
        fan_out_var="fan",
    )()
    pipeline: Pipeline[Any] = Pipeline(fan, cache_dir=tmp_path / "cache")
    with pytest.raises(ValueError, match="same text"):
        pipeline.execute(dispatcher=SubprocessDispatcher())
