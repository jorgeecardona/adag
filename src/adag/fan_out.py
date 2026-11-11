"""FanOutStep — declarative per-key sub-pipeline iteration.

Apply a sub-pipeline factory to each value in a dict, collecting the resolved
results into a dict keyed by the same keys. The factory is closed over the
parent scope so it can reference shared upstream Steps (a catalog, a lookup
sources, etc.) — the per-branch sub-DAG resolves under the same Pipeline
context, so per-item caches are shared across the parent and every fan-out
branch.

Example::

    per_op_notes = FanOutStep(
        over={"ground_acs": ground_cfg, "cabin_safety": cabin_cfg},
        factory=lambda key, value: _op_branch(key, value).notes_regenerated,
    )()

The factory MUST return a ``StepResult[V]`` (i.e. the result of calling a
configured Step). ``FanOutStep.run()`` is not the entry point — execution
flows through ``execute(context, ...)`` so each sub-DAG can resolve under
the same ``ResolveContext`` that drives the parent pipeline.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Hashable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

import structlog

from adag.deferred import BranchRef, InlineDispatcher, ResolveContext
from adag.step import Step, StepResult

logger = structlog.get_logger()

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class FanOutBranchError(RuntimeError):
    """One or more distributed fan-out branches failed.

    Succeeded branches' results are already in the shared cache (written by their
    workers, keyed by content hash), so a rerun re-dispatches only the failed keys.
    """

    def __init__(self, succeeded: list[str], failed: dict[str, str]) -> None:
        self.succeeded = succeeded
        self.failed = failed
        detail = "; ".join(f"{key}: {error}" for key, error in failed.items())
        super().__init__(
            f"{len(failed)} fan-out branch(es) failed ({detail}); {len(succeeded)} succeeded."
        )


@dataclass(frozen=True, eq=False)
class FanOutStep(Step[dict[K, V]], Generic[K, V]):
    """Apply a sub-pipeline factory to each value in ``over``; collect into a dict.

    Fields:
        over: dict whose keys identify each fan-out branch and whose values
            are passed verbatim to ``factory`` for that branch.
        factory: callable ``(key, value) -> StepResult[V]`` building the
            sub-DAG for one branch. Called once per key during ``execute``;
            each returned ``StepResult`` is resolved under the same
            ``ResolveContext`` as the parent so caches are shared.

    The optional ``label`` field overrides the visualization label.
    """

    over: dict[K, Any]
    factory: Callable[[K, Any], StepResult[V]]
    # Concurrency of the in-process (InlineDispatcher) path. None (default) keeps the
    # historical sequential branch-by-branch resolution. An int resolves branches in a
    # ThreadPoolExecutor of that size — a win when each branch is I/O-bound (an LLM/HTTP
    # call). Shared upstreams stay safe under it: ResolveContext's in-flight dedup runs a
    # shared node's body once even when many branch workers reach it at the same instant.
    # Ignored by a non-inline dispatcher, which has its own concurrency.
    max_workers: int | None = field(default=None)
    # Optional human-readable label for visualization. Mirrors the
    # ClassVar on Step but as a per-instance override so a notebook can
    # name two FanOuts differently without subclassing.
    instance_label: str = field(default="")
    # For distributed dispatch (e.g. SubprocessDispatcher): the absolute path of the
    # module that defines this fan-out and the module-level variable its
    # StepResult is bound to, so a worker can re-import and re-derive a branch
    # via factory(key, over[key]). Empty for in-process runs (InlineDispatcher),
    # which never need them. Not part of content_hash (it ignores fields).
    pipeline_module: str = field(default="")
    fan_out_var: str = field(default="")

    def execute(self, context: ResolveContext, /, **kwargs: Any) -> dict[K, V]:
        """Resolve one sub-DAG per key in ``over``.

        In-process (InlineDispatcher, the default) each branch resolves under the parent
        context: branch-by-branch when ``max_workers`` is None (identical to the pre-dispatch
        behaviour) or concurrently in a thread pool when it is set. With a real dispatcher,
        branches are dispatched to workers and rejoined through the shared cache by content hash.
        """
        del kwargs  # FanOutStep does not take wired inputs — closure over factory does.
        if isinstance(context.dispatcher, InlineDispatcher):
            if self.max_workers is None:
                return self._resolve_branches_sequentially(context)
            return self._resolve_branches_concurrently(context, self.max_workers)
        return self._dispatch_branches(context)

    def _resolve_branches_sequentially(self, context: ResolveContext) -> dict[K, V]:
        """Resolve each branch under the parent context, one after another."""
        results: dict[K, V] = {}
        # Hold every branch StepResult alive for the whole fan-out: the per-execute memo keys by
        # id(), so a branch freed before the next is built would let CPython reuse its address and
        # the next branch would hit its stale memo entry.
        branches: list[StepResult[V]] = []
        for key, value in self.over.items():
            # Tell the observer which branch is next so it can label this branch's
            # subtree by key (a no-op for every observer except RunTreeObserver).
            context.observer.on_fan_out_branch(str(key))
            sub = self._branch(key, value)
            branches.append(sub)
            results[key] = sub.resolve(context)
        return results

    def _resolve_branches_concurrently(
        self, context: ResolveContext, max_workers: int
    ) -> dict[K, V]:
        """Resolve every branch in a thread pool, each worker inside a COPIED context so ambient
        ContextVars (the usage observer, ``current_observer``) propagate across the thread boundary.

        A branch raising propagates out of ``future.result()`` — same as the sequential path, where a
        branch raising aborts ``execute`` — rather than being collected like the distributed path. The
        pool's ``__exit__`` waits for the in-flight branches before the exception unwinds.
        """
        if not self.over:
            return {}
        prepared: list[tuple[K, StepResult[V]]] = []
        for key, value in self.over.items():
            context.observer.on_fan_out_branch(str(key))
            prepared.append((key, self._branch(key, value)))
        results: dict[K, V] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures: dict[Future[V], K] = {
                pool.submit(copy_context().run, sub.resolve, context): key for key, sub in prepared
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return {key: results[key] for key, _ in prepared}  # keep the order of ``over``

    def _branch(self, key: K, value: Any) -> StepResult[V]:
        """Build one branch's sub-DAG via the factory, validating it is a resolvable Deferred."""
        sub = self.factory(key, value)
        if not isinstance(sub, StepResult):  # pyright: ignore[reportUnnecessaryIsInstance]  (factory is user code)
            raise TypeError(
                f"FanOutStep factory must return a StepResult/Deferred; got {type(sub).__name__} for key {key!r}."
            )
        return sub

    def _dispatch_branches(self, context: ResolveContext) -> dict[K, V]:
        """Distribute branches via ``context.dispatcher`` and rejoin by content hash.

        Pass 1 re-derives each branch (runs the factory), validates it can cross
        the wire, and builds a ``BranchRef``. Pass 2 dispatches all branches
        concurrently — each ``dispatch`` blocks on its worker / cache, so a thread
        pool runs N branches on N machines in parallel (the polling is I/O-bound).
        """
        if not self.pipeline_module or not self.fan_out_var:
            raise ValueError(
                "Distributed FanOutStep requires pipeline_module and fan_out_var so a worker can re-derive branches; "
                "set them when building the fan-out for distributed dispatch."
            )

        seen: dict[str, K] = {}
        for key in self.over:
            if str(key) in seen:
                raise ValueError(
                    f"FanOutStep keys {seen[str(key)]!r} and {key!r} have the same text "
                    f"{str(key)!r}; a worker finds its branch by that text, so keys must differ."
                )
            seen[str(key)] = key

        prepared: list[tuple[K, StepResult[V], BranchRef]] = []
        for key, value in self.over.items():
            context.observer.on_fan_out_branch(str(key))
            sub = self.factory(key, value)
            if not isinstance(sub, StepResult):  # pyright: ignore[reportUnnecessaryIsInstance]  (factory is user code)
                raise TypeError(
                    f"FanOutStep factory must return a StepResult; got {type(sub).__name__} for key {key!r}."
                )
            if sub.step.result_model() is None:
                raise TypeError(
                    f"Distributed FanOutStep branch {key!r} ({type(sub.step).__name__}) has no result_type; "
                    "distributed branches must terminate in a step whose result is a BaseModel (so it can cross the cache)."
                )
            branch_hash = sub.content_hash(params=context.params)
            if branch_hash is None:
                raise ValueError(
                    f"FanOutStep branch {key!r} has no content hash (blocked input); cannot dispatch."
                )
            prepared.append(
                (key, sub, BranchRef(self.pipeline_module, self.fan_out_var, str(key), branch_hash))
            )

        if not prepared:
            return {}

        results: dict[K, V] = {}
        failed: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
            futures = {
                pool.submit(context.dispatcher.dispatch, ref, sub, context): key
                for key, sub, ref in prepared
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as exc:
                    failed[str(key)] = str(exc)
                    context.observer.on_fan_out_branch_failed(str(key), str(exc))

        if failed:
            raise FanOutBranchError(succeeded=sorted(str(key) for key in results), failed=failed)
        return {key: results[key] for key, _, _ in prepared}  # keep the order of ``over``

    def content_hash(self) -> str | None:
        """Identity = class + each branch's sub-DAG content_hash.

        ``factory`` itself can't be hashed (closures aren't stably
        hashable), so we materialize each branch once and fold the
        resulting ``StepResult.content_hash()`` into our own digest.
        Keys are included so reordering / renaming branches invalidates
        the cache; values only contribute via the sub-DAG hash they
        produce — that's what actually matters for correctness.

        Returns None when a branch's hash is blocked (e.g. unsatisfied
        upstream FileSource), so the parent pipeline behaves like any
        other unsatisfied step.
        """
        h = hashlib.sha256()
        h.update(b"FANOUT:" + type(self).__name__.encode())
        for key in sorted(self.over, key=repr):
            try:
                sub = self.factory(key, self.over[key])
            except Exception as exc:
                logger.warning(
                    "fan_out.factory_raised",
                    step=type(self).__name__,
                    key=repr(key),
                    error_type=type(exc).__name__,
                    error=str(exc)[:200],
                )
                return None
            sub_hash = sub.content_hash() if isinstance(sub, StepResult) else None  # pyright: ignore[reportUnnecessaryIsInstance]
            if sub_hash is None:
                return None
            h.update(b"|K:" + repr(key).encode() + b"|H:" + sub_hash.encode())
        return h.hexdigest()[:12]
