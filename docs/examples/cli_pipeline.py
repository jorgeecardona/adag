"""A pipeline file for `adag run`: module-level step results become targets."""

from dataclasses import dataclass

from pydantic import BaseModel

from adag import InputParam, Step


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class Square(Step[Number]):
    def run(self, x: int) -> Number:
        return Number(value=x * x)


@dataclass(frozen=True)
class AddOne(Step[Number]):
    def run(self, n: Number) -> Number:
        return Number(value=n.value + 1)


x = InputParam[int]("x", value_type=int)
squared = Square()(x=x)
total = AddOne()(n=squared)
