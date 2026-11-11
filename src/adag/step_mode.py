"""Step operating mode — TRAIN / EVAL / INFERENCE.

Used by trainable pipelines to switch torch modules between train and eval
behavior without re-instantiating them. Consumers can accept any StepMode-ish
value via duck typing on `str(mode) == "train"` or `getattr(mode, "name", "") == "TRAIN"`.
"""

from enum import StrEnum


class StepMode(StrEnum):
    """Operating mode for a step."""

    TRAIN = "train"
    EVAL = "eval"
    INFERENCE = "inference"
