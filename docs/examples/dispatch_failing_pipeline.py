"""A pipeline file whose branch "b" fails in the worker."""

from dataclasses import dataclass

from pydantic import BaseModel

from adag import FanOutStep, Step


class Parsed(BaseModel):
    value: int


@dataclass(frozen=True)
class Parse(Step[Parsed]):
    text: str

    def run(self, text: str) -> Parsed:
        if not text:
            raise ValueError("row b has no value")
        return Parsed(value=int(text))


def branch(key: str, text: str):
    return Parse(text=text)()


per_key = FanOutStep(
    over={"a": "1", "b": "", "c": "3"},
    factory=branch,
    pipeline_module=__file__,
    fan_out_var="per_key",
)()
