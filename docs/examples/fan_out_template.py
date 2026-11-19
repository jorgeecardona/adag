"""Fan out over the result of an upstream step: one branch per part that Split returns."""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import FanOutStep, InputParam, NullObserver, Pipeline, Step, StepResult, Template

measured: list[str] = []


class Size(BaseModel):
    chars: int


@dataclass(frozen=True)
class Split(Step[dict[str, str]]):
    """Cut the text at each semicolon. The number of branches depends on the text."""

    def run(self, text: str) -> dict[str, str]:
        return {f"part{i}": part for i, part in enumerate(text.split(";"))}


@dataclass(frozen=True)
class Measure(Step[Size]):
    text: str

    def run(self, text: str) -> Size:
        measured.append(text)
        return Size(chars=len(text))


def measure(key: str, item: str) -> StepResult[Size]:
    """Build the sub-graph for one branch. Pass `item` on to a step; do not read it here."""
    return Measure(text=item)()


text = InputParam[str]("text", value_type=str)
sizes = FanOutStep[str, Size](template=Template(build=measure))(over=Split()(text=text))

with tempfile.TemporaryDirectory() as cache:
    pipeline = Pipeline(sizes, cache_dir=Path(cache))
    for text_value in [
        "Hello.;A longer paragraph.;Bye.",
        "Hello.;A longer paragraph.;Bye.",
        "Hello.;A longer paragraph.;See you.",
    ]:
        measured.clear()
        result = pipeline.execute(observer=NullObserver(), text=text_value)
        print({key: size.chars for key, size in result.items()}, "measured:", measured)
