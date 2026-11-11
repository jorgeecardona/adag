"""Deprecated names from the first import still work, and warn on use."""

import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import adag
from adag import InputParam, Pipeline, Step
from adag.deprecation import AdagDeprecationWarning
from adag.hashing import hash_list, hash_scalar


class Out(BaseModel):
    text: str


@dataclass(frozen=True)
class Echo(Step[Out]):
    def run(self, code: str) -> Out:
        return Out(text=code)


def test_parameter_of_builds_a_typed_input_param_and_warns(tmp_path: Path) -> None:
    with pytest.warns(AdagDeprecationWarning, match="adag.Parameter"):
        param_cls: Any = adag.Parameter
    with pytest.warns(AdagDeprecationWarning, match="InputParam"):
        code = param_cls.of("code", str)
    assert code == InputParam("code", value_type=str)
    result = Pipeline(Echo()(code=code), cache_dir=tmp_path).execute(code="007")
    assert result.text == "007"


def test_parameter_of_with_default() -> None:
    from adag.parameter import Parameter

    with pytest.warns(AdagDeprecationWarning, match="InputParam"):
        param = Parameter.of("threshold", float, default=0.5)
    assert param.has_default and param.default == 0.5 and param.value_type is float


def test_step_mode_import_warns_and_has_three_values() -> None:
    sys.modules.pop("adag.step_mode", None)
    with pytest.warns(AdagDeprecationWarning, match="StepMode"):
        module = importlib.import_module("adag.step_mode")
    assert [m.value for m in module.StepMode] == ["train", "eval", "inference"]


def test_branch_execution_error_still_raisable() -> None:
    from adag.dispatch import BranchExecutionError

    err = BranchExecutionError("k", "exit 1")
    assert err.key == "k" and "exit 1" in str(err)


def test_import_adag_does_not_warn(recwarn: pytest.WarningsRecorder) -> None:
    importlib.reload(adag)
    assert not [
        w for w in recwarn if issubclass(w.category, (DeprecationWarning, AdagDeprecationWarning))
    ]


def test_hash_helpers_keep_first_release_bytes() -> None:
    assert hash_scalar({"b": 1, "a": 2}) == json.dumps({"a": 2, "b": 1}).encode()
    assert hash_list([3, "x"]) == b'[3, "x"]'


@dataclass(frozen=True)
class Typed(Step[Out]):
    produces = "text"
    accepts = ("text", "html")

    def run(self) -> Out:
        return Out(text="")


def test_accepts_from_checks_ports() -> None:
    step = Typed()
    assert step.accepts_from("html")
    assert not step.accepts_from("image")
    assert step.accepts_from(Typed())
    assert Echo().accepts_from("anything")  # untyped side accepts all
