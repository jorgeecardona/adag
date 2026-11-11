"""Deprecated: ``Parameter.of`` builds an ``InputParam``. Use ``InputParam`` directly.

Old code keeps working::

    text = Parameter.of("text", str)
    threshold = Parameter.of("threshold", float, default=0.5)

New code::

    text = InputParam("text", value_type=str)
    threshold = InputParam.with_default("threshold", 0.5)
"""

from __future__ import annotations

import warnings
from typing import Any, TypeVar

from adag.deferred import InputParam
from adag.deprecation import AdagDeprecationWarning

T = TypeVar("T")

_UNSET = object()


class Parameter:
    """Deprecated factory for ``InputParam``. Removed in 1.0."""

    @staticmethod
    def of(name: str, parameter_type: type[T], default: Any = _UNSET) -> InputParam[T]:
        """Return an ``InputParam`` with ``value_type=parameter_type`` and an optional default."""
        warnings.warn(
            "Parameter.of is deprecated and is removed in adag 1.0. Use "
            "InputParam(name, value_type=T) or InputParam.with_default(name, default) instead.",
            AdagDeprecationWarning,
            stacklevel=2,
        )
        if default is _UNSET:
            return InputParam(name=name, value_type=parameter_type)
        return InputParam.with_default(name, default, value_type=parameter_type)
