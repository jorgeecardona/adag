# pyright: reportIncompatibleMethodOverride=true
"""Override idiom from the docs, checked by pyright strict (#14).

This file runs as a normal test. The first line turns ``reportIncompatibleMethodOverride`` on for
this file, because pyproject.toml turns it off for the repo. The type check (``pyright`` over
``tests``) then fails if a named parameter override of a step hook is reported. There is no
``pyright: ignore`` comment on any override below: that is the point of the file.
"""

import asyncio
from dataclasses import dataclass

from pydantic import BaseModel

from adag import AsyncStep, BatchItem, BatchStep, Step


class Result(BaseModel):
    text: str


class Results(BaseModel):
    items: list[Result]


@dataclass(frozen=True)
class Repeat(Step[Result]):
    times: int = 2

    def run(self, text: str, times: int) -> Result:
        return Result(text=text * times)


@dataclass(frozen=True)
class Upper(AsyncStep[Result]):
    async def arun(self, text: str) -> Result:
        return Result(text=text.upper())


@dataclass(frozen=True)
class Greet(BatchStep[Results, Result]):
    items_source_keys = ("names",)

    def items_data(self, names: list[str]) -> list[str]:
        return names

    def process_item(self, item: BatchItem, names: list[str]) -> Result:
        del names
        return Result(text=f"hi {item.data}")

    def assemble(self, results: list[Result], names: list[str]) -> Results:
        del names
        return Results(items=results)


def test_named_parameter_overrides_run() -> None:
    assert Repeat().run(text="ab", times=2).text == "abab"


def test_named_parameter_override_of_arun() -> None:
    assert asyncio.run(Upper().arun(text="ab")).text == "AB"


def test_named_parameter_overrides_of_the_batch_hooks() -> None:
    step = Greet()
    assert step.items_data(names=["a"]) == ["a"]
    assert step.assemble([Result(text="x")], names=["a"]).items[0].text == "x"
