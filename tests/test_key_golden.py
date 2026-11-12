"""Golden cache keys: literal values that pin the key format.

A change to any value here changes every cache key a user has on disk. If a test fails, either
the change was a mistake, or it is deliberate: then bump ``adag.hashing.KEY_FORMAT`` and say so
in the CHANGELOG.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel

from adag import Config, FileSource, InputParam, SerializableStep, Step, StepResult


class Num(BaseModel):
    value: int


@dataclass(frozen=True)
class Plain(Step[Num]):
    a: int = 1
    b: str = "x"

    def run(self, a: int, b: str) -> Num:
        return Num(value=a)


@dataclass(frozen=True)
class Pinned(Step[Num]):
    cache_key = "pinnedkey001"
    a: int = 1

    def run(self, a: int) -> Num:
        return Num(value=a)


@dataclass(frozen=True)
class Kinded(SerializableStep[Num]):
    kind = "golden.kinded"
    a: int = 2

    def run(self, a: int) -> Num:
        return Num(value=a)


@dataclass(frozen=True)
class WithConfig(Step[Num]):
    a: int = 1
    workers: Config[int] = Config(4)

    def run(self, a: int, workers: int) -> Num:
        return Num(value=a)


@dataclass(frozen=True)
class Labelled(Step[Num]):
    a: int = 1
    node_label: str = ""

    def run(self, a: int) -> Num:
        return Num(value=a)


@dataclass(frozen=True)
class WithFile(Step[Num]):
    data: FileSource

    def run(self, data: str) -> Num:
        return Num(value=len(data))


@dataclass(frozen=True)
class WithNested(Step[Num]):
    inner: Plain

    def run(self) -> Num:
        return Num(value=0)


@dataclass(frozen=True)
class Down(Step[Num]):
    def run(self, up: Num, p: int, q: int, raw: str) -> Num:
        return Num(value=up.value + p + q)


@pytest.fixture
def data_file(tmp_path: Path) -> Path:
    path = tmp_path / "data.txt"
    path.write_text("golden\n")
    return path


def _graph() -> StepResult[Num]:
    return Down()(
        up=Plain()(),
        p=InputParam("p"),
        q=InputParam.with_default("q", 3),
        raw="literal",
    )


def test_step_keys_are_pinned(data_file: Path) -> None:
    assert Plain().content_hash() == GOLDEN["plain"]
    assert Plain(a=2).content_hash() == GOLDEN["plain_a2"]
    assert Pinned().content_hash() == "pinnedkey001"
    assert Kinded().content_hash() == GOLDEN["kinded"]
    assert WithConfig(workers=Config(99)).content_hash() == GOLDEN["config"]
    assert WithFile(data=FileSource(str(data_file))).content_hash() == GOLDEN["file"]
    assert WithNested(inner=Plain()).content_hash() == GOLDEN["nested"]
    assert Labelled(node_label="shown").content_hash() == GOLDEN["labelled"]
    assert Labelled(node_label="other").content_hash() == GOLDEN["labelled"]


def test_node_keys_are_pinned() -> None:
    node = _graph()
    assert node.content_hash(params={"p": 5}) == GOLDEN["node_p5"]
    assert _graph().content_hash(params={"p": 5, "q": 7}) == GOLDEN["node_p5_q7"]
    assert _graph().content_hash(params=None) == GOLDEN["node_no_params"]


# Computed at 0.2.35. Class bodies above feed the keys (class source is hashed): editing a
# fixture class changes its key on purpose.
GOLDEN: dict[str, str] = {
    "plain": "a38a5e0bb6f3",
    "plain_a2": "9b03d1bbe3e1",
    "labelled": "d18f358453c1",
    "kinded": "c095f725ab62",
    "config": "bc9e5eb9829c",
    "file": "7766806d8b7c",
    "nested": "be55daf5f9fc",
    "node_p5": "7f0a2a9a98de",
    "node_p5_q7": "cb90d5d8f1f8",
    "node_no_params": "c21d05822526",
}
