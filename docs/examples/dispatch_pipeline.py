"""A pipeline file whose fan-out branches can run in worker processes."""

import os
from dataclasses import dataclass

from pydantic import BaseModel

from adag import FanOutStep, Step


class Doubled(BaseModel):
    value: int
    pid: int


@dataclass(frozen=True)
class Double(Step[Doubled]):
    n: int

    def run(self, n: int) -> Doubled:
        return Doubled(value=n * 2, pid=os.getpid())


def branch(key: str, n: int):
    return Double(n=n)()


per_key = FanOutStep(
    over={"a": 1, "b": 2, "c": 3},
    factory=branch,
    pipeline_module=__file__,  # workers re-import this file ...
    fan_out_var="per_key",  # ... and find the fan-out by this name
)()
