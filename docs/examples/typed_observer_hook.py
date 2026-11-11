"""A typed domain hook: a step sends a value, a custom observer receives it."""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from adag import InputParam, NullObserver, Pipeline, Step
from adag.deferred import PipelineObserver, ResolveContext, current_observer


@runtime_checkable
class ScoreObserver(Protocol):
    """Declares the extra hook. Any observer that has this method receives scores."""

    def on_score(self, score: float) -> None: ...


def report_score(observer: PipelineObserver, score: float) -> None:
    """Call ``on_score`` when the observer has it; do nothing otherwise."""
    if isinstance(observer, ScoreObserver):
        observer.on_score(score)


def normalise(x: float) -> float:
    """A helper deep in the call stack: it reaches the observer of the current run."""
    score = x / 10
    report_score(current_observer(), score)
    return score


class Number(BaseModel):
    value: float


@dataclass(frozen=True)
class Score(Step[Number]):
    def run(self, x: float) -> Number:
        return Number(value=normalise(x))


@dataclass(frozen=True)
class Double(Step[Number]):
    def execute(self, context: ResolveContext, **inputs: object) -> Number:
        result = super().execute(context, **inputs)
        report_score(context.observer, result.value)  # a step with the context in hand
        return result

    def run(self, n: Number) -> Number:
        return Number(value=n.value * 2)


@dataclass
class CollectScores(NullObserver):
    scores: list[float] = field(default_factory=list[float])

    def on_score(self, score: float) -> None:
        self.scores.append(score)


pipeline = Pipeline(Double()(n=Score()(x=InputParam[float]("x"))))
observer = CollectScores()
pipeline.execute(x=7.0, observer=observer)
print(observer.scores)
pipeline.execute(x=7.0, observer=NullObserver())  # a plain observer: report_score does nothing
