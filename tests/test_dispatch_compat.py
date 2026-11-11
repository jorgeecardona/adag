"""Dispatch code written against 0.1 still runs: one-branch dispatchers, positional fields, --param."""

from pathlib import Path
from typing import Any

import pytest

from adag.cli import load_pipeline_module, main, run_branch
from adag.deferred import BranchRef, InlineDispatcher, ResolveContext
from adag.dispatch import BranchPolicy
from adag.pipeline import Pipeline
from adag.step import StepResult

PIPELINE_SOURCE = """
from dataclasses import dataclass

from pydantic import BaseModel

from adag import FanOutStep, InputParam, Step


class Doubled(BaseModel):
    value: int


@dataclass(frozen=True)
class Double(Step[Doubled]):
    n: int

    def run(self, n: int, bump: int) -> Doubled:
        return Doubled(value=n * 2 + bump)


bump = InputParam("bump", value_type=int)


def _branch(key, value):
    return Double(n=value)(bump=bump)


per_key = FanOutStep(
    over={"a": 1, "b": 2},
    factory=_branch,
    pipeline_module=__file__,
    fan_out_var="per_key",
)()
"""


class OneBranchDispatcher:
    """A 0.1-style dispatcher: only ``dispatch``, no ``dispatch_all``."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    def dispatch(self, branch: BranchRef, sub: StepResult[Any], context: ResolveContext) -> Any:
        self.keys.append(branch.key)
        return sub.resolve(context)


def _pipeline(tmp_path: Path) -> Path:
    path = tmp_path.joinpath("pipeline.py")
    path.write_text(PIPELINE_SOURCE)
    return path


def test_one_branch_dispatcher_still_resolves_a_fan_out(tmp_path: Path) -> None:
    module = load_pipeline_module(_pipeline(tmp_path))
    dispatcher = OneBranchDispatcher()
    pipeline = Pipeline(module.per_key, cache_dir=tmp_path / "cache")
    with pytest.warns(DeprecationWarning, match="dispatch_all"):
        result = pipeline.execute(bump=1, dispatcher=dispatcher)  # pyright: ignore[reportArgumentType]
    assert {k: v.value for k, v in result.items()} == {"a": 3, "b": 5}
    assert sorted(dispatcher.keys) == ["a", "b"]


def test_inline_dispatcher_keeps_one_branch_dispatch(tmp_path: Path) -> None:
    module = load_pipeline_module(_pipeline(tmp_path))
    sub = module.per_key.step.factory("a", 1)
    ref = BranchRef("p", "per_key", "a", "h")
    result = InlineDispatcher().dispatch(ref, sub, ResolveContext(params={"bump": 0}))
    assert result.value == 2


def test_resolve_context_positional_fields_keep_their_first_release_meaning() -> None:
    def callback(progress: Any) -> None: ...

    context = ResolveContext(Path("/tmp/x"), {}, callback)
    assert context.on_progress is callback
    assert context.store is None


def test_branch_policy_positional_fields_keep_their_first_release_meaning() -> None:
    policy = BranchPolicy(1, 60, 2.0)
    assert (policy.max_retries, policy.timeout_seconds, policy.poll_interval_seconds) == (
        1,
        60,
        2.0,
    )


def test_run_branch_accepts_key_value_list(tmp_path: Path) -> None:
    path = _pipeline(tmp_path)
    with pytest.warns(DeprecationWarning, match="KEY=VALUE"):
        result = run_branch(path, "per_key", "b", ["bump=3"], tmp_path / "cache")
    assert result.value == 7


def test_resolve_branch_accepts_deprecated_param_flag(tmp_path: Path) -> None:
    path = _pipeline(tmp_path)
    argv = ["resolve-branch", str(path), "--fan-out", "per_key", "--key", "a"]
    argv += ["--cache-dir", str(tmp_path / "cache"), "--param", "bump=4"]
    with pytest.warns(DeprecationWarning, match="KEY=VALUE"):
        assert main(argv) == 0
