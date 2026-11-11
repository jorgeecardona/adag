"""Deprecated: ``StepMode`` (TRAIN, EVAL, INFERENCE). Define your own enum. Removed in 1.0.

adag never reads a step's mode. This enum is kept so code ported from the internal package
imports.
"""

import warnings
from enum import StrEnum

warnings.warn(
    "adag.step_mode is deprecated; define your own StepMode enum",
    DeprecationWarning,
    stacklevel=2,
)


class StepMode(StrEnum):
    """Operating mode for a step."""

    TRAIN = "train"
    EVAL = "eval"
    INFERENCE = "inference"
