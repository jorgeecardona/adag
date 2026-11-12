"""Deprecated: ``StepMode`` (TRAIN, EVAL, INFERENCE). Define your own enum. Removed in 1.0.

adag never reads a step's mode. This enum is kept so code written for the first release
imports.
"""

import warnings
from enum import StrEnum

from adag.deprecation import AdagDeprecationWarning

warnings.warn(
    "adag.step_mode is deprecated and is removed in adag 1.0. Define your own StepMode enum "
    "instead: adag never reads it.",
    AdagDeprecationWarning,
    stacklevel=2,
)


class StepMode(StrEnum):
    """Operating mode for a step."""

    TRAIN = "train"
    EVAL = "eval"
    INFERENCE = "inference"
