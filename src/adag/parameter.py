"""Parameter — backward-compatible alias for InputParam.

New code should use InputParam directly:

    text = InputParam("text")
    threshold = InputParam.with_default("threshold", 0.5)

Old code using Parameter.of() continues to work:

    text = Parameter.of("text", str)
    threshold = Parameter.of("threshold", float, default=0.5)
"""

from __future__ import annotations

from typing import Any, TypeVar

from adag.deferred import InputParam

T = TypeVar("T")

_UNSET = object()


class Parameter:
    """Backward-compatible factory for InputParam."""

    @staticmethod
    def of(name: str, parameter_type: type[T], default: Any = _UNSET) -> InputParam[T]:
        """Create an InputParam with an explicit type and optional default."""
        if default is _UNSET:
            return InputParam(name=name)
        return InputParam.with_default(name, default)
