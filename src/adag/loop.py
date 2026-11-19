"""``LoopStep``: repeat a sub-graph, feeding each round's result into the next.

A DAG has no cycles, so adag does not draw one. A ``LoopStep`` is one node that builds its rounds
when the run reaches it. Round ``n`` is the sub-graph built over the state that round ``n - 1``
returned, and it is cached under the key of that sub-graph. A rerun finds every finished round in
the cache, and a loop that stopped half way continues after its last cached round.

Example::

    sqrt2 = LoopStep(
        body=Template(inputs=("state",), build=lambda state: Newton(state=state)()),
        until=is_converged,
        max_rounds=30,
    )(init=Start()())

The loop stops at the first of these:

1. ``until(state)`` is true. It is tested on the start state too, so a loop can run zero rounds.
2. A round returns the state it was given (a fixed point). Not tested with ``key_by_round``.
3. ``max_rounds`` rounds have run. ``LoopNotConverged`` is raised, unless ``on_max="return"``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from adag.deferred import ResolveContext
from adag.hashing import encode_value
from adag.key import Expansion
from adag.step import Step, StepResult
from adag.template import Template

_NO_INIT = "LoopStep needs a start state: wire it as init, LoopStep(...)(init=node), or pass init=."


class LoopNotConverged(RuntimeError):
    """A ``LoopStep`` ran ``max_rounds`` rounds and ``until`` was still false.

    ``rounds`` is the number of rounds that ran and ``state`` is the state after the last one.
    The rounds are cached, so a rerun with a higher ``max_rounds`` continues from them.
    """

    def __init__(self, rounds: int, state: Any) -> None:
        self.rounds = rounds
        self.state = state
        super().__init__(
            f"the loop did not stop after {rounds} round(s). Raise max_rounds to continue from "
            "the cached rounds, or pass on_max='return' to take the last state."
        )


def _identity(until: Callable[[Any], bool]) -> str:
    """The qualified name of ``until``, or a ``TypeError`` when a name cannot identify it.

    A lambda or a function defined inside another function has no stable name: two different
    ones can share it, so a change to the predicate would not change the loop's key.
    """
    name = getattr(until, "__qualname__", None)
    module = getattr(until, "__module__", None)
    if not name or not module or "<lambda>" in name or "<locals>" in name:
        raise TypeError(
            "LoopStep until must be a function defined at module level: adag hashes its name, and "
            "a lambda or a nested function has none that is stable. Change until_version when its "
            "behaviour changes."
        )
    return f"{module}.{name}"


@dataclass(frozen=True, eq=False, kw_only=True)
class LoopStep(Step[Any]):
    """Run ``body`` again and again over its own result until ``until(state)`` is true.

    The state is what the body returns. It must be a ``BaseModel`` or JSON-like data, so adag can
    hash it and tell whether a round changed it.

    Fields:
        body: a ``Template`` with the input ``("state",)``, or ``("state", "round")`` when
            ``key_by_round`` is true. It returns the sub-graph for one round.
        init: the start state. Wire an upstream node, ``LoopStep(...)(init=node)``, or pass a
            value here.
        until: a function ``state -> bool`` defined at module level. Its qualified name is part of
            the loop's key; change ``until_version`` when its behaviour changes.
        until_version: a text you change when ``until`` changes.
        max_rounds: the most rounds to run. It is not part of a round's key, so raising it reuses
            every round that already ran.
        on_max: ``"raise"`` (default) raises ``LoopNotConverged`` at ``max_rounds``; ``"return"``
            returns the last state.
        key_by_round: put the round number into each round's key and skip the fixed-point stop.
            Use it when the same state must be tried again, for example a retry. Without it,
            the same state gives the same key, and the cache returns the same result.
        instance_label: overrides the visualization label.
    """

    body: Template
    until: Callable[[Any], bool]
    init: Any = None
    until_version: str = ""
    max_rounds: int = 20
    on_max: Literal["raise", "return"] = "raise"
    key_by_round: bool = False
    instance_label: str = field(default="")

    def __post_init__(self) -> None:
        _identity(self.until)
        wanted = ("state", "round") if self.key_by_round else ("state",)
        if self.body.inputs != wanted:
            raise TypeError(
                f"LoopStep body needs the inputs {wanted} with key_by_round={self.key_by_round}; "
                f"got {self.body.inputs}."
            )
        if self.max_rounds < 0:
            raise ValueError(f"LoopStep max_rounds must be 0 or more; got {self.max_rounds}.")
        if self.on_max not in ("raise", "return"):
            raise ValueError(f"LoopStep on_max must be 'raise' or 'return'; got {self.on_max!r}.")

    def __call__(self, **kwargs: Any) -> StepResult[Any]:
        """Wire this loop into the DAG. ``init`` can be an upstream node, passed here or as a field.

        Raises:
            TypeError: ``init`` is given twice, as a field and as an input.
        """
        wired: Any = kwargs.get("init", self.init)
        if not isinstance(wired, StepResult):
            return super().__call__(**kwargs)
        if "init" in kwargs and isinstance(self.init, StepResult):
            raise TypeError("LoopStep got init= twice: as a field and as an input.")
        step = dataclasses.replace(self, init=None)
        return StepResult(step=step, sources={**kwargs, "init": wired})

    def execute(self, context: ResolveContext, /, **kwargs: Any) -> Any:
        """Run the rounds in this process and return the final state.

        Raises:
            LoopNotConverged: ``max_rounds`` rounds ran and ``until`` was still false.
            TypeError: there is no start state, or a state cannot be encoded as a cache key.
        """
        state: Any = kwargs.get("init", self.init)
        if state is None:
            raise TypeError(_NO_INIT)
        encoded = _encode(state, "the start state")
        rounds = 0
        while True:
            if self.until(state):
                return state
            if rounds == self.max_rounds:
                break
            sub = self._round(rounds, state)
            context.observer.on_loop_round(rounds, sub.content_hash(params=context.params) or "")
            following: Any = sub.resolve(context)
            rounds += 1
            following_encoded = _encode(following, f"the state after round {rounds}")
            if not self.key_by_round and following_encoded == encoded:
                return following
            state, encoded = following, following_encoded
        if self.on_max == "return":
            return state
        raise LoopNotConverged(rounds, state)

    async def aexecute(self, context: ResolveContext, /, **kwargs: Any) -> Any:
        """Async entry: run ``execute`` in a worker thread so the event loop is not blocked."""
        return await asyncio.to_thread(self.execute, context, **kwargs)

    def _round(self, number: int, state: Any) -> StepResult[Any]:
        """The sub-graph of one round, built over the state."""
        if self.key_by_round:
            return self.body.bind(state, number)
        return self.body.bind(state)

    def expansion(self) -> Expansion | None:
        """A loop builds its rounds at run time."""
        return Expansion(
            shape="loop",
            template=self.body,
            note=(
                f"up to {self.max_rounds} rounds; each round is cached under the key of the "
                "state it starts from"
            ),
        )

    def content_hash(self) -> str | None:
        """Identity = the body template, ``until`` and its version, the limits, and a static init.

        A wired ``init`` is not here: the node key adds the key of the upstream node. ``max_rounds``
        is in the loop's key, not in a round's key.
        """
        body_hash = self.body.hash()
        if body_hash is None:
            return None
        h = hashlib.sha256()
        h.update(b"LOOP:" + type(self).__name__.encode())
        h.update(b"|BODY:" + body_hash.encode())
        h.update(b"|UNTIL:" + encode_value(_identity(self.until)))
        h.update(b"|VERSION:" + encode_value(self.until_version))
        h.update(b"|MAX:" + encode_value(self.max_rounds))
        h.update(b"|ON_MAX:" + encode_value(self.on_max))
        h.update(b"|BY_ROUND:" + encode_value(self.key_by_round))
        h.update(b"|INIT:" + encode_value(self.init, "the LoopStep init"))
        return h.hexdigest()[:12]


def _encode(state: Any, where: str) -> bytes:
    """The state as cache-key bytes. Two states are the same when these bytes are equal."""
    return encode_value(state, f"{where} of a LoopStep")
