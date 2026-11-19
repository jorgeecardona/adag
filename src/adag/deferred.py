"""Deferred[T]: a value that doesn't exist yet, resolved at execution time.

The core abstraction of the DAG engine. All values in the graph are Deferred:
parameters, file sources, and step results.

    text = InputParam("text")
    threshold = InputParam.with_default("threshold", 0.5)
    tokens = tokenizer(text=text)     # StepResult (Deferred)
    pipeline = Pipeline(tokens, cache_dir=Path("cache"))
    pipeline.execute(text="hello")
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import warnings
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, get_args, get_origin

from adag.deprecation import AdagDeprecationWarning

if TYPE_CHECKING:
    from adag.cache_store import CacheStore
    from adag.step import Step, StepResult

T = TypeVar("T")
_S = TypeVar("_S")


def _empty_params() -> dict[str, Any]:
    return {}


def _empty_resolved() -> dict[int, Any]:
    return {}


def _empty_inflight() -> dict[int | str, Event]:
    return {}


def _empty_ainflight() -> dict[int | str, asyncio.Future[None]]:
    return {}


@dataclass(frozen=True)
class Progress:
    """Progress report from a resolving Deferred."""

    step: str
    message: str
    fraction: float | None = None
    data: Any = None


class PipelineObserver(Protocol):
    """Hooks a pipeline run calls so you can watch it: logs, timing, progress.

    ``StepResult.resolve`` calls ``on_step_start`` before it resolves a step's inputs, and
    ``on_step_cached`` or ``on_step_finished`` after. A child's hooks fall between its parent's
    start and finish. ``RunTreeObserver`` builds its tree from that order.

    Subclass ``NullObserver`` and override the hooks you need. For a value of your own (a score,
    a vector), declare a typed hook: see "Typed hooks for your own values" in docs/observers.md.
    """

    def on_step_start(self, step: str) -> None: ...

    def on_step_cached(self, step: str) -> None: ...

    def on_step_finished(self, step: str, seconds: float) -> None: ...

    def on_fan_out_branch(self, key: str) -> None: ...

    def on_fan_out_branch_failed(self, key: str, error: str) -> None: ...

    def on_loop_round(self, number: int, key: str) -> None: ...

    def on_event(self, event: str, fields: dict[str, Any]) -> None: ...


class NullObserver:
    """Silent observer: every hook is a no-op. Use it to turn the per-step
    logging off. Concrete observers inherit this and override only the hooks
    they care about, so new hooks never break an existing observer.
    """

    def on_step_start(self, step: str) -> None:
        pass

    def on_step_cached(self, step: str) -> None:
        pass

    def on_step_finished(self, step: str, seconds: float) -> None:
        pass

    def on_fan_out_branch(self, key: str) -> None:
        pass

    def on_fan_out_branch_failed(self, key: str, error: str) -> None:
        pass

    def on_loop_round(self, number: int, key: str) -> None:
        pass

    def on_event(self, event: str, fields: dict[str, Any]) -> None:
        pass

    def on_embedding(self, vector: list[float]) -> None:
        """Deprecated no-op, kept for code that calls it. Use a typed hook (docs/observers.md)."""
        _warn_domain_hook("on_embedding")

    def on_model_refused(self, model: str) -> None:
        """Deprecated no-op, kept for code that calls it. Use a typed hook (docs/observers.md)."""
        _warn_domain_hook("on_model_refused")


_WARNED_DOMAIN_HOOKS: set[str] = set()


def _warn_domain_hook(name: str) -> None:
    """Warn once per process: these hooks are called per item, and ``-W error`` must not fire
    on every call.
    """
    if name in _WARNED_DOMAIN_HOOKS:
        return
    _WARNED_DOMAIN_HOOKS.add(name)
    warnings.warn(
        f"NullObserver.{name} is deprecated, does nothing, and is removed in adag 1.0. "
        "Declare a typed hook instead "
        '(see "Typed hooks for your own values" in docs/observers.md)',
        AdagDeprecationWarning,
        stacklevel=3,
    )


_CURRENT_OBSERVER: contextvars.ContextVar[PipelineObserver] = contextvars.ContextVar(
    "pipeline_observer",
    default=NullObserver(),  # noqa: B039  (NullObserver is stateless; sharing one instance is safe)
)


def current_observer() -> PipelineObserver:
    """The observer bound for the in-flight step, or a NullObserver outside any run.

    Set for the duration of a step body by :func:`observing` (which ``Step.execute`` enters), so nested
    asyncio tasks and worker threads can emit hooks
    without threading an observer argument through every signature. Propagates across ``asyncio.to_thread``
    and asyncio tasks automatically; a raw ``ThreadPoolExecutor`` submit must run inside a copied
    context (``contextvars.copy_context().run``) to inherit it.
    """
    return _CURRENT_OBSERVER.get()


def emit(event: str, **fields: Any) -> None:
    """Send a progress event (cache writes, batch progress, dispatch) to the run's observer.

    Outside a run there is no observer, so the event is dropped. Warnings and errors are
    logged with structlog directly instead, so an observer can never hide them.
    """
    send_event(current_observer(), event, fields)


def send_event(observer: PipelineObserver, event: str, fields: dict[str, Any]) -> None:
    """Call ``observer.on_event`` when it has one; skip an observer written before the hook existed.

    ``on_event`` was added in 0.2.6. An observer that implements the Protocol without inheriting
    ``NullObserver`` may not have it, and a progress event must never crash a run.
    """
    hook = getattr(observer, "on_event", None)
    if hook is not None:
        hook(event, fields)


@contextlib.contextmanager
def observing(observer: PipelineObserver) -> Generator[None]:
    """Bind ``observer`` as the ambient :func:`current_observer` for the duration of the block."""
    token = _CURRENT_OBSERVER.set(observer)
    try:
        yield
    finally:
        _CURRENT_OBSERVER.reset(token)


class Executor(Protocol):
    """Decides *where* a step runs. The graph stays the same; the executor is a
    property of the run, carried on ``ResolveContext``.

    ``run`` receives the step, the active context, and the already-resolved input
    values, and must return the step's result. The default ``LocalExecutor`` runs
    it in-process. A remote executor would instead dispatch the step (by its
    content hash) to another process or machine, wait for the result to land in
    the shared cache, and return it, without the graph knowing the difference.
    """

    def run(self, step: Step[Any], context: ResolveContext, inputs: dict[str, Any]) -> Any: ...


class LocalExecutor:
    """Runs each step in this process. The default."""

    def run(self, step: Step[Any], context: ResolveContext, inputs: dict[str, Any]) -> Any:
        return step.execute(context, **inputs)


class AsyncExecutor(Protocol):
    """The async counterpart of ``Executor``: decides *where* a step runs on the ASYNC resolve path
    (``StepResult.aresolve`` / ``Pipeline.aexecute``). Carried on ``ResolveContext.async_executor``.

    Mirrors ``Executor`` exactly except ``run`` is awaitable, so a step body that ``await``s I/O (a DB
    session, an HTTP/LLM call) runs ON the event loop, no thread offload, so the event loop stays
    responsive and cancellation propagates through the awaits. The default ``LocalAsyncExecutor`` runs the
    step in-process; a remote async executor would dispatch by content hash and await the result.
    """

    async def run(
        self, step: Step[Any], context: ResolveContext, inputs: dict[str, Any]
    ) -> Any: ...


class LocalAsyncExecutor:
    """Awaits each step's async body in-process, the default async executor. The mirror of
    ``LocalExecutor`` for the ``aresolve`` path.
    """

    async def run(self, step: Step[Any], context: ResolveContext, inputs: dict[str, Any]) -> Any:
        return await step.aexecute(context, **inputs)


@dataclass(frozen=True)
class BranchRef:
    """Everything a worker needs to re-derive and resolve one fan-out branch.

    The worker imports ``pipeline_module``, finds the ``FanOutStep`` bound to
    ``fan_out_var``, looks up its own ``over[key]`` (so the value never has to
    serialize), calls ``factory(key, value)`` or binds the template to rebuild the branch
    sub-DAG (so the closure never serializes either), and resolves it. When ``over`` is an
    upstream node, the worker resolves that node first. ``branch_hash`` is the
    branch ``StepResult``'s content hash: the cache key the worker writes to and
    the main process polls/loads from. ``params`` are the run's ``InputParam``
    values, which the worker needs to resolve to the same hash.
    """

    pipeline_module: str
    fan_out_var: str
    key: str
    branch_hash: str


class BranchDispatcher(Protocol):
    """Decides where the branches of one fan-out are resolved.

    ``FanOutStep`` calls ``dispatch_all`` once per fan-out with every branch. A dispatcher
    returns ``{branch.key: result}``, or raises ``FanOutBranchError`` naming the failed keys.
    A worker writes its result to the shared cache under ``branch.branch_hash``, and the
    dispatcher loads it from there. The default ``InlineDispatcher`` resolves in-process.
    """

    def dispatch_all(
        self, branches: Sequence[tuple[BranchRef, StepResult[Any]]], context: ResolveContext
    ) -> dict[str, Any]: ...


class InlineDispatcher:
    """Resolves each branch in-process, one after another. The default.

    ``FanOutStep`` does not call it: it resolves inline branches itself, so it can honour
    ``max_workers``. ``dispatch_all`` is here so the class meets the protocol.
    """

    def dispatch_all(
        self, branches: Sequence[tuple[BranchRef, StepResult[Any]]], context: ResolveContext
    ) -> dict[str, Any]:
        return {ref.key: sub.resolve(context) for ref, sub in branches}

    def dispatch(self, branch: BranchRef, sub: StepResult[Any], context: ResolveContext) -> Any:
        """Resolve one branch in-process. Kept from the pre-0.2.11 one-branch protocol."""
        del branch
        return sub.resolve(context)


@dataclass(frozen=True)
class ResolveContext:
    """The state of one run: where results are cached, the params, the observer and the memo.

    Build one per run and pass it to every ``resolve``, ``aresolve`` or ``aresolve_all`` call.

    - ``cache_dir``: files for step results. ``store`` replaces it with any ``CacheStore``. With
      neither, nothing is cached.
    - ``params``: the ``InputParam`` values of the run.
    - ``observer``: ``NullObserver`` by default here. ``Pipeline.execute`` passes
      ``LoggingObserver`` unless you give another.
    - The memo: each node resolves once per context, so every downstream step gets the same
      object. Threads or tasks that reach one node at the same time run its body once.
    """

    cache_dir: Path | None = None
    params: dict[str, Any] = field(default_factory=_empty_params)
    on_progress: Callable[[Progress], None] | None = None
    observer: PipelineObserver = field(default_factory=NullObserver)
    executor: Executor = field(default_factory=LocalExecutor)
    async_executor: AsyncExecutor = field(default_factory=LocalAsyncExecutor)
    dispatcher: BranchDispatcher = field(default_factory=InlineDispatcher)
    # Last, so positional ResolveContext(cache_dir, params, on_progress, ...) from 0.1 still binds.
    store: CacheStore | None = None
    # The memo: ``id(node) -> (node, value)``. Without it a step that returns a non-serializable
    # object (a model handle) would run once per consumer, and each consumer would get a
    # different instance.
    _resolved: dict[int, Any] = field(default_factory=_empty_resolved, repr=False, compare=False)
    # While one thread runs a node's body, its content key maps to an Event that other threads
    # wait on (the id of a node with no key). Two node objects with one key share the entry. The
    # owner removes the entry and sets the Event in a ``finally``, so a body that raises releases
    # its waiters, which then resolve the node again.
    _inflight: dict[int | str, Event] = field(
        default_factory=_empty_inflight, repr=False, compare=False
    )
    # Guards every read and write of the two dicts above. A level of nodes resolves in a thread
    # pool, and the free-threaded build gives no atomicity for free.
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)
    # The in-flight registry for ``aresolve``: a future per node, so gathered tasks wait on the
    # loop instead of blocking it on a thread Event.
    _ainflight: dict[int | str, asyncio.Future[None]] = field(
        default_factory=_empty_ainflight, repr=False, compare=False
    )

    def result_store(self) -> CacheStore | None:
        """Where step results are cached: ``store``, else files under ``cache_dir``, else None."""
        if self.store is not None:
            return self.store
        if self.cache_dir is None:
            return None
        from adag.cache_store import FileSystemCacheStore  # late: cache_store imports io

        return FileSystemCacheStore(self.cache_dir)


class Deferred(Generic[T]):
    """A value of type T that doesn't exist yet. Resolved at execution time.

    Class attribute:
        deferred_type: the concrete type T, auto-set when inheriting Deferred[ConcreteType].
    """

    deferred_type: type[Any] | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        for parent in getattr(cls, "__orig_bases__", []):
            origin = get_origin(parent)
            if origin is Deferred:
                # Direct Deferred[ConcreteType] base: auto-detect.
                args = get_args(parent)
                if args and isinstance(args[0], type) and "deferred_type" not in cls.__dict__:
                    cls.deferred_type = args[0]
                return

        # No direct Deferred[X] base. Check for diamond: multiple Deferred subclass bases
        # with no explicit deferred_type declaration are ambiguous.
        deferred_bases: list[Any] = []
        for parent in getattr(cls, "__orig_bases__", []):
            raw: Any = get_origin(parent) or parent
            if isinstance(raw, type) and issubclass(raw, Deferred) and raw is not Deferred:
                deferred_bases.append(raw)

        if len(deferred_bases) > 1 and "deferred_type" not in cls.__dict__:
            names = ", ".join(b.__name__ for b in deferred_bases)
            raise TypeError(
                f"'{cls.__name__}' inherits from multiple Deferred subclasses ({names}) "
                f"but does not declare 'deferred_type'. "
                f"Add 'deferred_type = <type>' to resolve the ambiguity."
            )

    def resolve(self, context: ResolveContext) -> T:
        raise NotImplementedError

    async def aresolve(self, context: ResolveContext) -> T:
        """Async resolution. The default delegates to the synchronous ``resolve``, correct for LEAF
        Deferreds whose resolution does no awaitable work (``InputParam`` reads ``context.params``,
        ``FileSource`` reads bytes). ``StepResult`` overrides this to await async step bodies and resolve
        upstreams on the event loop, so an existing sync graph runs unchanged on the async path.
        """
        return self.resolve(context)


@dataclass(frozen=True)
class InputParam(Deferred[T]):
    """A named runtime parameter. Use with_default() for optional parameters.

    ```python
    text = InputParam("text", value_type=str)
    threshold = InputParam.with_default("threshold", 0.5)
    ```

    ``value_type`` tells the CLI how to parse ``--param``. It is needed on a required parameter,
    because ``InputParam[int]("x")`` loses the ``int`` at runtime. It is not part of the cache key.
    """

    name: str
    default: T | None = None
    has_default: bool = False
    value_type: type[Any] | None = None

    @staticmethod
    def with_default(name: str, default: _S, value_type: type[Any] | None = None) -> InputParam[_S]:
        """Create a parameter with a default value (even if the default is None)."""
        param: Any = InputParam(name=name, default=default, has_default=True, value_type=value_type)
        return param

    def resolve(self, context: ResolveContext) -> T:
        if self.name in context.params and context.params[self.name] is not None:
            return context.params[self.name]
        if self.has_default:
            default_value: Any = self.default
            return default_value
        raise ValueError(f"Missing required parameter: {self.name}")
