"""StepOutput — a reference to a step's output in the DAG.

When you call a Step, it returns a StepOutput that represents the
result of executing that step with those inputs. StepOutputs wire
into other steps as inputs, forming the DAG.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from adag.parameter import Parameter

T = TypeVar("T")


@dataclass(frozen=True)
class StepOutput(Generic[T]):
    """A reference to a step's output in the DAG.

    Created by calling a Step. Carries the step instance and the
    inputs it was called with. Used as input to downstream steps.
    """

    step: object
    inputs: dict[str, StepOutput[object] | Parameter | object]

    def __repr__(self) -> str:
        return f"StepOutput({type(self.step).__name__})"
