"""A pipeline file with a slow step, so a branch is still running when the main process looks."""

import time
from dataclasses import dataclass

from pydantic import BaseModel

from adag import FanOutStep, Step


class Doubled(BaseModel):
    value: int


@dataclass(frozen=True)
class SlowDouble(Step[Doubled]):
    n: int

    def run(self, n: int) -> Doubled:
        time.sleep(4)
        return Doubled(value=n * 2)


def branch(key: str, n: int):
    return SlowDouble(n=n)()


per_key = FanOutStep(
    over={"a": 1, "b": 2},
    factory=branch,
    pipeline_module=__file__,
    fan_out_var="per_key",
)()
