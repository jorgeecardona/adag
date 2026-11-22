"""Frames: draw the body of a fan-out and of a loop once, inside a dashed frame."""

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import FanOutStep, InputParam, LoopStep, Step, StepResult, Template, dag
from adag.visualization import SvgOptions


class Size(BaseModel):
    chars: int


@dataclass(frozen=True)
class Split(Step[dict[str, str]]):
    """Cut the text at each semicolon."""

    def run(self, text: str) -> dict[str, str]:
        return {f"part{i}": part for i, part in enumerate(text.split(";"))}


@dataclass(frozen=True)
class Measure(Step[Size]):
    """Count the characters of one part."""

    text: str

    def run(self, text: str) -> Size:
        return Size(chars=len(text))


@dataclass(frozen=True)
class Halve(Step[Size]):
    """Cut the size in half."""

    state: Size

    def run(self, state: Size) -> Size:
        return Size(chars=state.chars // 2)


@dataclass(frozen=True)
class Start(Step[Size]):
    """The first state."""

    def run(self) -> Size:
        return Size(chars=1000)


def measure(key: str, item: str) -> StepResult[Size]:
    """Build the sub-graph for one branch."""
    return Measure(text=item)()


def halve(state: Size) -> StepResult[Size]:
    """Build one round."""
    return Halve(state=state)()


def small(state: Size) -> bool:
    """The stop test."""
    return state.chars < 10


text = InputParam[str]("text", value_type=str)
fan_out = FanOutStep[str, Size](template=Template(build=measure))(over=Split()(text=text))
loop = LoopStep(body=Template(inputs=("state",), build=halve), until=small)(init=Start()())

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
if out.suffix == ".svg":  # the example test passes one file path: use its folder
    out = out.parent
dag(fan_out, path=out / "frame_fan_out.svg", options=SvgOptions())
dag(loop, path=out / "frame_loop.svg", options=SvgOptions(style="band"))
dag(fan_out, path=out / "frame_off.svg", options=SvgOptions(frames=False))
print("wrote 3 graphs")
