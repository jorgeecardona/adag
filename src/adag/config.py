"""Config wrapper — execution parameters excluded from the content hash.

Use for parameters that change how a step runs but not what it produces:
concurrency, batch size, timeout, device, tracker, etc.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Config(Generic[T]):
    """Wrap execution-only parameters that do NOT affect the output hash.

    Usage in step fields::

        @dataclass(frozen=True)
        class Stage1Candidates(CachedStep[Stage1Result]):
            model: str = "sonnet"                       # hashed — affects output
            concurrency: Config[int] = Config(20)       # NOT hashed — execution only
            batch_size: Config[int] = Config(256)       # NOT hashed — execution only
            tracker: Config[UsageTracker | None] = Config(None)

    In ``run()``, the value is unwrapped automatically::

        def run(self, model: str, concurrency: int, batch_size: int) -> Stage1Result:
            ...

    Generic over ``T`` so you can wrap anything an execution-time knob
    can hold — primitives, objects, optional handles. The framework's
    ``content_hash()`` skips ``Config`` fields entirely, so the wrapped
    type's hashability doesn't matter.
    """

    __slots__ = ("value",)

    # Stored as Any so isinstance(x, Config) narrowing doesn't leave
    # `.value` as Unknown (which then poisons every downstream access).
    # Users get their declared T back through how Step.resolve unwraps
    # Config fields when calling run(): the field's declared
    # ``Config[T]`` gives the run() parameter its T-typed kwarg
    # automatically.
    value: Any

    def __init__(self, value: T) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"Config({self.value!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Config):
            return NotImplemented
        return self.value == other.value

    def __hash__(self) -> int:
        try:
            return hash(self.value)
        except TypeError:
            # Unhashable wrapped value (mutable container, custom class
            # without __hash__). Fall back to identity — Config is mostly
            # used as a frozen-dataclass field where two instances
            # holding equivalent values are constructed once at the call
            # site, so identity-hash is sufficient.
            return id(self.value)
