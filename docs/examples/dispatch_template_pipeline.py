"""A pipeline file whose branches come from an upstream step and can run in worker processes."""

import os
from dataclasses import dataclass

from pydantic import BaseModel

from adag import FanOutStep, InputParam, Step, Template


class Size(BaseModel):
    chars: int
    pid: int


@dataclass(frozen=True)
class Split(Step[dict[str, str]]):
    """Cut the text at each semicolon. The number of branches depends on the text."""

    def run(self, text: str) -> dict[str, str]:
        return {f"part{i}": part for i, part in enumerate(text.split(";"))}


@dataclass(frozen=True)
class Measure(Step[Size]):
    text: str

    def run(self, text: str) -> Size:
        return Size(chars=len(text), pid=os.getpid())


def measure(key: str, item: str):
    return Measure(text=item)()


text = InputParam[str]("text", value_type=str)
sizes = FanOutStep(
    template=Template(build=measure),
    pipeline_module=__file__,  # workers re-import this file ...
    fan_out_var="sizes",  # ... and find the fan-out by this name
)(over=Split()(text=text))
