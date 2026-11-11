"""CachedStep: a deprecated alias of ``Step``.

Every ``Step`` with a ``BaseModel`` result is cached through the pipeline, and strict
content hashing is the default, so ``CachedStep`` adds nothing. Subclass ``Step``.
"""

from __future__ import annotations

import warnings
from typing import Any, TypeVar, get_origin

from pydantic import BaseModel

from adag.step import Step

CT = TypeVar("CT", bound=BaseModel)


class CachedStep(Step[CT]):
    """Deprecated: subclass ``Step[...]`` instead. Behaves the same."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if any(get_origin(base) is CachedStep for base in getattr(cls, "__orig_bases__", [])):
            warnings.warn(
                f"{cls.__name__}: CachedStep is deprecated; subclass Step[...] instead.",
                DeprecationWarning,
                stacklevel=2,
            )
