from dataclasses import dataclass

from pydantic import BaseModel

from adag import Config, InputParam, Step


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class Scale(Step[Number]):
    factor: int = 2
    workers: Config[int] = Config(4)

    def run(self, x: int, factor: int, workers: int) -> Number:
        return Number(value=x * factor)


node = Scale()(x=InputParam[int]("x"))
print(node.explain_key({"x": 3}))
