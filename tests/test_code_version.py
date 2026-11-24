"""``code_version`` pins the code part of a key, so a harmless source edit keeps the key (#19)."""

import importlib.util
import itertools
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import InputParam, Step
from adag.hashing import class_source_hash
from adag.key import ComponentKind


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class Plain(Step[Number]):
    def run(self, x: int) -> Number:
        return Number(value=x)


@dataclass(frozen=True)
class Pinned(Step[Number]):
    code_version: str = "v1"  # type: ignore[assignment]

    def run(self, x: int) -> Number:
        return Number(value=x)


_COUNTER = itertools.count()


def _make(
    tmp_path: Path, version: str | None, body: str = "return Number(value=x)"
) -> type[Step[Number]]:
    """A step class read from a real file, so ``inspect`` can see its source."""
    pin = f'    code_version = "{version}"\n' if version is not None else ""
    path = tmp_path / f"made_{next(_COUNTER)}.py"
    path.write_text(
        "from dataclasses import dataclass\n"
        "from adag import Step\n"
        "from test_code_version import Number\n\n\n"
        "@dataclass(frozen=True)\n"
        "class Made(Step[Number]):\n"
        f"{pin}"
        "    def run(self, x: int) -> Number:\n"
        f"        {body}\n"
    )
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module.Made  # type: ignore[no-any-return]


def _key(step: Step[Number]) -> str | None:
    return step(x=InputParam[int]("x")).explain_key({"x": 1}).key


def _code_row(step: Step[Number]) -> tuple[str, str]:
    rows = step(x=InputParam[int]("x")).explain_key({"x": 1}).components
    row = next(r for r in rows if r.kind is ComponentKind.CODE)
    return row.name, row.value


def test_the_source_row_shows_the_whole_digest() -> None:
    name, value = _code_row(Plain())
    assert name == "ast"
    assert value == (class_source_hash(Plain) or b"").decode()
    assert len(value) == 12


def test_a_pin_replaces_the_digest_and_names_itself() -> None:
    assert _code_row(Pinned()) == ("code_version", "v1")


def test_a_changed_pin_changes_the_key(tmp_path: Path) -> None:
    assert _key(_make(tmp_path, "v1")()) != _key(_make(tmp_path, "v2")())


def test_a_source_edit_under_a_pin_keeps_the_key(tmp_path: Path) -> None:
    before = _make(tmp_path, "v1")
    after = _make(tmp_path, "v1", "return Number(value=x + 0)")
    assert class_source_hash(before) != class_source_hash(after)
    assert _key(before()) == _key(after())


def test_without_a_pin_the_same_edit_moves_the_key(tmp_path: Path) -> None:
    before = _make(tmp_path, None)
    after = _make(tmp_path, None, "return Number(value=x + 0)")
    assert _key(before()) != _key(after())


def test_the_digest_as_a_pin_leaves_the_key_unchanged(tmp_path: Path) -> None:
    plain = _make(tmp_path, None)
    digest = (class_source_hash(plain) or b"").decode()
    pinned = _make(tmp_path, digest, "return Number(value=x + 0)")
    assert _key(plain()) == _key(pinned())


def test_a_subclass_does_not_inherit_the_pin() -> None:
    @dataclass(frozen=True)
    class Child(Pinned):
        def run(self, x: int) -> Number:
            return Number(value=x + 1)

    assert _code_row(Child())[0] == "ast"


@pytest.mark.parametrize("bad", ["", 3])
def test_a_pin_that_is_not_a_text_is_refused(bad: object) -> None:
    @dataclass(frozen=True)
    class Bad(Step[Number]):
        def run(self, x: int) -> Number:
            return Number(value=x)

    Bad.code_version = bad  # type: ignore[assignment]
    with pytest.raises(TypeError, match="code_version"):
        _key(Bad())
