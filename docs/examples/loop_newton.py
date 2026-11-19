"""LoopStep: Newton iteration for the square root of 2, one cached round at a time."""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import LoopNotConverged, LoopStep, NullObserver, Pipeline, Step, StepResult, Template

TARGET = 2.0
computed: list[float] = []


class Guess(BaseModel):
    x: float


@dataclass(frozen=True)
class Start(Step[Guess]):
    """The first guess."""

    def run(self) -> Guess:
        return Guess(x=1.0)


@dataclass(frozen=True)
class Improve(Step[Guess]):
    """One Newton step: the mean of x and TARGET / x."""

    guess: Guess

    def run(self, guess: Guess) -> Guess:
        computed.append(guess.x)
        return Guess(x=(guess.x + TARGET / guess.x) / 2)


def converged(state: Guess) -> bool:
    """The stop test. It is a function at module level, so adag can hash its name."""
    return abs(state.x * state.x - TARGET) < 1e-12


def one_round(state: Guess) -> StepResult[Guess]:
    """Build one round. Pass `state` on to a step; do not read it here."""
    return Improve(guess=state)()


def sqrt2(max_rounds: int) -> StepResult[Guess]:
    body = Template(inputs=("state",), build=one_round)
    return LoopStep(body=body, until=converged, max_rounds=max_rounds)(init=Start()())


with tempfile.TemporaryDirectory() as cache:
    try:
        Pipeline(sqrt2(3), cache_dir=Path(cache)).execute(observer=NullObserver())
    except LoopNotConverged as stop:
        print(f"stopped after {stop.rounds} rounds, x = {stop.state.x:.6f}, ran {len(computed)}")
    computed.clear()
    result = Pipeline(sqrt2(20), cache_dir=Path(cache)).execute(observer=NullObserver())
    print(f"x = {result.x:.12f} after {len(computed)} new rounds")
    computed.clear()
    Pipeline(sqrt2(20), cache_dir=Path(cache)).execute(observer=NullObserver())
    print(f"rerun: {len(computed)} new rounds")
