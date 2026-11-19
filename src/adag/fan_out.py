"""FanOutStep: one sub-graph per key of a dict.

``FanOutStep`` applies a factory to each value of a dict and returns a dict of the results
with the same keys. The factory can use shared upstream steps from the parent scope. Each
branch resolves under the same ``ResolveContext`` as the parent pipeline, so per-item caches
are shared between the parent and every branch.

Example::

    sizes = FanOutStep(
        over={"intro": "Hello.", "body": "A longer paragraph."},
        factory=lambda key, text: Measure(text=text)(),
    )()

The factory must return a ``StepResult[V]``, the result of calling a configured step.

``over`` can also be an upstream node, with a ``Template`` that builds each branch from the key
and the item. The keys are then known when the run reaches the fan-out::

    sizes = FanOutStep(template=Template(build=lambda key, item: Measure(text=item)()))(over=split)

``FanOutStep.run()`` is not the entry point: execution goes through ``execute(context, ...)``
so each sub-graph resolves under the same ``ResolveContext`` that drives the parent pipeline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import warnings
from collections.abc import Callable, Hashable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, TypeVar

import structlog

from adag.deferred import BranchRef, InlineDispatcher, ResolveContext, gather_first_failure
from adag.deprecation import AdagDeprecationWarning, warn_deprecated
from adag.hashing import encode_value
from adag.key import Expansion
from adag.step import Step, StepResult
from adag.template import Template

logger = structlog.get_logger()

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

_FACTORY_CANNOT_BE_WIRED = (
    "FanOutStep(over=<node>) needs template=: a factory cannot be hashed, so the key would not "
    "change when the factory does."
)
_TEMPLATE_INPUTS = 2  # a fan-out template takes (key, item)


def _no_items() -> dict[Any, Any]:
    return {}


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

    Give either ``factory`` (keys known when the graph is built) or ``template`` (keys can come
    from an upstream node). With ``template``, wire ``over`` as an input,
    ``FanOutStep(template=t)(over=node)``, or pass the node as ``over=``.

    Fields:
        over: dict whose keys identify each fan-out branch and whose values
            are passed verbatim to ``factory`` or ``template`` for that branch. With a
            ``template`` it can be an upstream ``StepResult`` of a dict, resolved at run time.
            Each item must be encodable as a cache key (a ``BaseModel``, or JSON-like data).
        factory: callable ``(key, value) -> StepResult[V]`` building the
            sub-DAG for one branch. Called once per key during ``execute``;
            each returned ``StepResult`` is resolved under the same
            ``ResolveContext`` as the parent so caches are shared.
        template: a ``Template`` with inputs ``("key", "item")``, used instead of ``factory``.
            A branch key is the key of the sub-graph built over that item, so an upstream that
            returns the same items again finds every branch in the cache.
        max_workers: run branches in-process on this many threads. Ignored by a dispatcher.
            Deprecated, removed in 1.0: use ``max_concurrency`` and ``runs_on``.
        instance_label: overrides the visualization label.
        pipeline_module: file path or module name a dispatch worker imports.
        fan_out_var: module-level variable that holds this fan-out's ``StepResult``.
    """

    _orchestrates: ClassVar[bool] = True

    over: dict[K, Any] | StepResult[dict[K, Any]] = field(default_factory=_no_items)
    factory: Callable[[K, Any], StepResult[V]] | None = None
    # Concurrency of the in-process (InlineDispatcher) path. None (default) keeps the
    # historical sequential branch-by-branch resolution. An int resolves branches in a
    # ThreadPoolExecutor of that size, a win when each branch is I/O-bound (an LLM/HTTP
    # call). Shared upstreams stay safe under it: ResolveContext's in-flight dedup runs a
    # shared node's body once even when many branch workers reach it at the same instant.
    # Ignored by a non-inline dispatcher, which has its own concurrency.
    max_workers: int | None = field(default=None)
    # Optional human-readable label for visualization. Mirrors the
    # ClassVar on Step but as a per-instance override so a notebook can
    # name two FanOuts differently without subclassing.
    instance_label: str = field(default="")
    # For distributed dispatch (e.g. SubprocessDispatcher): the file path or the module
    # name (pkg.pipeline) of the module that defines this fan-out and the module-level variable its
    # StepResult is bound to, so a worker can re-import and re-derive a branch
    # via factory(key, over[key]) or the template. Empty for in-process runs (InlineDispatcher),
    # which never need them. Not part of content_hash (it ignores fields).
    pipeline_module: str = field(default="")
    fan_out_var: str = field(default="")
    # Last, so positional FanOutStep(over, factory, max_workers, ...) from 0.1 still binds.
    template: Template | None = None

    def __post_init__(self) -> None:
        if self.max_workers is not None:
            warn_deprecated(
                "FanOutStep(max_workers=)",
                "Pipeline.aexecute(max_concurrency=) or aresolve_all(max_concurrency=), "
                'with runs_on = "thread" on the steps in the branches',
                stacklevel=3,
            )
        if (self.factory is None) == (self.template is None):
            raise TypeError("FanOutStep needs exactly one of factory= or template=.")
        if isinstance(self.over, StepResult) and self.template is None:
            raise TypeError(_FACTORY_CANNOT_BE_WIRED)
        if self.template is not None and len(self.template.inputs) != _TEMPLATE_INPUTS:
            raise TypeError(
                f"FanOutStep template needs two inputs (key, item); got {self.template.inputs}."
            )

    def __call__(self, **kwargs: Any) -> StepResult[dict[K, V]]:
        """Wire this fan-out into the DAG.

        ``over`` can be an upstream node, passed here as ``over=node`` or as the field. That form
        needs ``template=``: a ``factory`` is code adag cannot hash.

        Raises:
            TypeError: ``over`` is wired and there is no ``template``, or it is given twice.
        """
        wired: Any = kwargs.get("over", self.over)
        if not isinstance(wired, StepResult):
            return super().__call__(**kwargs)
        if self.template is None:
            raise TypeError(_FACTORY_CANNOT_BE_WIRED)
        if "over" in kwargs and isinstance(self.over, StepResult):
            raise TypeError("FanOutStep got over= twice: as a field and as an input.")
        with warnings.catch_warnings():  # the warning was given when the step was made
            warnings.simplefilter("ignore", AdagDeprecationWarning)
            step = dataclasses.replace(self, over={})
        return StepResult(step=step, sources={**kwargs, "over": wired})

    def execute(self, context: ResolveContext, /, **kwargs: Any) -> dict[K, V]:
        """Resolve one sub-DAG per key in ``over``.

        In-process (InlineDispatcher, the default) each branch resolves under the parent
        context: branch-by-branch when ``max_workers`` is None (identical to the pre-dispatch
        behaviour) or concurrently in a thread pool when it is set. With a real dispatcher,
        branches are dispatched to workers and rejoined through the shared cache by content hash.
        """
        over = self._items(kwargs)
        if isinstance(context.dispatcher, InlineDispatcher):
            if self.max_workers is None:
                return self._resolve_branches_sequentially(context, over)
            return self._resolve_branches_concurrently(context, over, self.max_workers)
        return self._dispatch_branches(context, over)

    def _items(self, kwargs: dict[str, Any]) -> dict[K, Any]:
        """The dict to fan out over: the resolved ``over`` input, else the ``over`` field.

        With a ``template`` the keys must differ as text and every item must be encodable as a
        cache key; both are checked before any branch runs.
        """
        over: Any = kwargs.get("over", self.over)
        if not isinstance(over, dict):
            raise TypeError(f"FanOutStep over must be a dict; got {type(over).__name__}.")
        items: dict[K, Any] = over  # pyright: ignore[reportUnknownVariableType]
        if self.template is not None:
            seen: dict[str, K] = {}
            for key, value in items.items():
                if str(key) in seen:
                    raise ValueError(
                        f"FanOutStep keys {seen[str(key)]!r} and {key!r} have the same text "
                        f"{str(key)!r}; keys must differ as text."
                    )
                seen[str(key)] = key
                encode_value(value, f"item {key!r} of the FanOutStep over")
        return items

    async def aexecute(self, context: ResolveContext, /, **kwargs: Any) -> dict[K, V]:
        """Async entry: resolve the branches as tasks of this run, so they share its limit.

        Each branch is an ``aresolve`` task. ``aresolve_all(max_concurrency=N)`` and
        ``Pipeline.aexecute(max_concurrency=N)`` count the step bodies inside the branches, and a
        branch can hold an ``AsyncStep``. A branch that raises cancels the others and raises as it
        is. With a dispatcher, or with ``max_workers`` set, ``execute`` runs in a worker thread
        instead. ``asyncio.to_thread`` copies the current context, so the observer reaches the
        branches.
        """
        if self.max_workers is not None or not isinstance(context.dispatcher, InlineDispatcher):
            return await asyncio.to_thread(self.execute, context, **kwargs)
        over = self._items(kwargs)
        prepared: list[tuple[K, StepResult[V]]] = []
        for key, value in over.items():
            context.observer.on_fan_out_branch(str(key))
            prepared.append((key, self._branch(key, value)))
        tasks = [asyncio.create_task(sub.aresolve(context)) for _, sub in prepared]
        try:
            values = await gather_first_failure(tasks)
        finally:
            for task in tasks:
                task.cancel()
        return {key: value for (key, _), value in zip(prepared, values, strict=True)}

    def _resolve_branches_sequentially(
        self, context: ResolveContext, over: Mapping[K, Any]
    ) -> dict[K, V]:
        """Resolve each branch under the parent context, one after another."""
        results: dict[K, V] = {}
        # Hold every branch StepResult alive for the whole fan-out: the per-execute memo keys by
        # id(), so a branch freed before the next is built would let CPython reuse its address and
        # the next branch would hit its stale memo entry.
        branches: list[StepResult[V]] = []
        for key, value in over.items():
            # Tell the observer which branch is next so it can label this branch's
            # subtree by key (a no-op for every observer except RunTreeObserver).
            context.observer.on_fan_out_branch(str(key))
            sub = self._branch(key, value)
            branches.append(sub)
            results[key] = sub.resolve(context)
        return results

    def _resolve_branches_concurrently(
        self, context: ResolveContext, over: Mapping[K, Any], max_workers: int
    ) -> dict[K, V]:
        """Resolve every branch in a thread pool, each worker inside a COPIED context so ambient
        ContextVars (the usage observer, ``current_observer``) propagate across the thread boundary.

        A branch raising propagates out of ``future.result()``, the same as on the sequential path,
        where a branch raising aborts ``execute``. It is not collected, as it is on the dispatcher path. The
        pool's ``__exit__`` waits for the in-flight branches before the exception unwinds.
        """
        if not over:
            return {}
        prepared: list[tuple[K, StepResult[V]]] = []
        for key, value in over.items():
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
        """Build one branch's sub-DAG via the template or factory, checking it is a StepResult."""
        if self.template is not None:
            return self.template.bind(key, value)
        sub = self._require_factory()(key, value)
        if not isinstance(sub, StepResult):  # pyright: ignore[reportUnnecessaryIsInstance]  (factory is user code)
            raise TypeError(
                f"FanOutStep factory must return a StepResult/Deferred; got {type(sub).__name__} for key {key!r}."
            )
        return sub

    def _dispatch_branches(self, context: ResolveContext, over: Mapping[K, Any]) -> dict[K, V]:
        """Distribute branches via ``context.dispatcher`` and rejoin by content hash.

        Pass 1 re-derives each branch (runs the factory, or binds the template), validates it can
        cross the wire, and builds a ``BranchRef``. Pass 2 hands all branches to
        ``dispatcher.dispatch_all`` in one call, so the dispatcher controls concurrency and polls
        every job in one loop.

        With a template, ``over`` is already resolved here, because it is an input of this step.
        A worker resolves the same upstream node again: it reads the result from the cache when
        the upstream result is a ``BaseModel``, and runs the upstream step otherwise.
        """
        if not self.pipeline_module or not self.fan_out_var:
            raise ValueError(
                "Distributed FanOutStep requires pipeline_module and fan_out_var so a worker can re-derive branches; "
                "set them when building the fan-out for distributed dispatch."
            )

        seen: dict[str, K] = {}
        for key in over:
            if str(key) in seen:
                raise ValueError(
                    f"FanOutStep keys {seen[str(key)]!r} and {key!r} have the same text "
                    f"{str(key)!r}; a worker finds its branch by that text, so keys must differ."
                )
            seen[str(key)] = key

        prepared: list[tuple[K, StepResult[V], BranchRef]] = []
        for key, value in over.items():
            context.observer.on_fan_out_branch(str(key))
            sub = self._branch(key, value)
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

        dispatch_all = getattr(context.dispatcher, "dispatch_all", None)
        if dispatch_all is None:
            return self._dispatch_one_by_one(context, prepared)
        by_key = dispatch_all([(ref, sub) for _, sub, ref in prepared], context)
        return {key: by_key[ref.key] for key, _, ref in prepared}  # keep the order of ``over``

    def _dispatch_one_by_one(
        self, context: ResolveContext, prepared: list[tuple[K, StepResult[V], BranchRef]]
    ) -> dict[K, V]:
        """Run a dispatcher that has only the pre-0.2.11 ``dispatch(branch, sub, context)``.

        Runs one thread per branch. Deprecated: implement ``dispatch_all``.
        """
        warnings.warn(
            f"{type(context.dispatcher).__name__} has only dispatch(); implement "
            "dispatch_all(branches, context). dispatch() support is removed in adag 1.0.",
            AdagDeprecationWarning,
            stacklevel=2,
        )
        dispatch: Any = getattr(context.dispatcher, "dispatch")  # noqa: B009
        results: dict[K, V] = {}
        failed: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
            futures = {
                pool.submit(copy_context().run, dispatch, ref, sub, context): key
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
        return {key: results[key] for key, _, _ in prepared}

    def expansion(self) -> Expansion | None:
        """A template fan-out builds its branches at run time; a factory fan-out does not."""
        if self.template is None:
            return None
        return Expansion(
            shape="mapped",
            template=self.template,
            note="one branch per item of 'over'; the number of branches is known at run time",
        )

    def content_hash(self) -> str | None:
        """Identity = class + each branch's sub-DAG content_hash.

        ``factory`` itself can't be hashed (closures aren't stably
        hashable), so we materialize each branch once and fold the
        resulting ``StepResult.content_hash()`` into our own digest.
        Keys are included so reordering / renaming branches invalidates
        the cache; values only contribute via the sub-DAG hash they
        produce. That is what matters for correctness.

        Returns None when a branch's hash is blocked (e.g. unsatisfied
        upstream FileSource), so the parent pipeline behaves like any
        other unsatisfied step.
        """
        h = hashlib.sha256()
        h.update(b"FANOUT:" + type(self).__name__.encode())
        if self.template is not None:
            return self._template_hash(h, self.template)
        factory, over = self._require_factory(), self._static_over()
        for key in sorted(over, key=repr):
            try:
                sub = factory(key, over[key])
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

    def _require_factory(self) -> Callable[[K, Any], StepResult[V]]:
        """The factory. A fan-out built with a template has none, and cannot be dispatched."""
        if self.factory is None:
            raise TypeError("this FanOutStep has a template, not a factory.")
        return self.factory

    def _static_over(self) -> dict[K, Any]:
        """The ``over`` field as a dict. A node is wired as an input, so the field is a dict."""
        if not isinstance(self.over, dict):
            raise TypeError("this FanOutStep wires over from a node, so it has no static dict.")
        return self.over

    def _template_hash(self, h: hashlib._Hash, template: Template) -> str | None:
        """The step hash of a template fan-out: the template hash and a static ``over``.

        A wired ``over`` is not here: the node key adds the key of the upstream node.
        """
        template_hash = template.hash()
        if template_hash is None:
            return None
        h.update(b"|TEMPLATE:" + template_hash.encode())
        h.update(b"|OVER:" + encode_value(self.over, "the FanOutStep over"))
        return h.hexdigest()[:12]
