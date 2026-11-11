"""Deferred[T] — a value that doesn't exist yet, resolved at execution time.

The core abstraction of the DAG engine. All values in the graph are Deferred:
parameters, file sources, and step results.

    text = InputParam("text")
    threshold = InputParam.with_default("threshold", 0.5)
    tokens = tokenizer(text=text)     # StepResult (Deferred)
    pipeline = Pipeline(tokens, cache_dir=Path("cache"))
    pipeline.execute(text="hello")
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, get_args, get_origin

if TYPE_CHECKING:
    from adag.step import Step, StepResult

T = TypeVar("T")
_S = TypeVar("_S")


def _empty_params() -> dict[str, Any]:
    return {}


def _empty_resolved() -> dict[int, Any]:
    return {}


def _empty_inflight() -> dict[int, Event]:
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

    Extension: when a step needs to expose a different kind of internal,
    add a new ``on_<thing>`` method here with plain-value arguments and a
    no-op default on ``NullObserver``. Existing observers ignore it
    automatically — provided they inherit ``NullObserver``.

    Lifecycle hooks (on_step_start / on_step_cached / on_step_finished) are
    emitted by ``StepResult.resolve`` for every step it reaches, so an observer
    can watch the whole DAG run, not just step-internal values. The depth-first
    resolve order brackets each child's start/finished inside its parent's,
    which is how ``RunTreeObserver`` reconstructs the execution tree.
    """

    def on_step_start(self, step: str) -> None: ...

    def on_step_cached(self, step: str) -> None: ...

    def on_step_finished(self, step: str, seconds: float) -> None: ...

    def on_fan_out_branch(self, key: str) -> None: ...

    def on_fan_out_branch_failed(self, key: str, error: str) -> None: ...

    def on_event(self, event: str, fields: dict[str, Any]) -> None: ...


class NullObserver:
    """Silent observer — every hook is a no-op. Use it to turn the per-step
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

    def on_event(self, event: str, fields: dict[str, Any]) -> None:
        pass


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
    current_observer().on_event(event, fields)


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
    the shared cache, and return it — without the graph knowing the difference.
    """

    def run(self, step: Step[Any], context: ResolveContext, inputs: dict[str, Any]) -> Any: ...


class LocalExecutor:
    """Runs each step in-process. The default — identical to the pre-seam behaviour."""

    def run(self, step: Step[Any], context: ResolveContext, inputs: dict[str, Any]) -> Any:
        return step.execute(context, **inputs)


class AsyncExecutor(Protocol):
    """The async counterpart of ``Executor`` — decides *where* a step runs on the ASYNC resolve path
    (``StepResult.aresolve`` / ``Pipeline.aexecute``). Carried on ``ResolveContext.async_executor``.

    Mirrors ``Executor`` exactly except ``run`` is awaitable, so a step body that ``await``s I/O (a DB
    session, an HTTP/LLM call) runs ON the event loop — no thread offload, so the event loop stays
    responsive and cancellation propagates through the awaits. The default ``LocalAsyncExecutor`` runs the
    step in-process; a remote async executor would dispatch by content hash and await the result.
    """

    async def run(
        self, step: Step[Any], context: ResolveContext, inputs: dict[str, Any]
    ) -> Any: ...


class LocalAsyncExecutor:
    """Awaits each step's async body in-process — the default async executor. The mirror of
    ``LocalExecutor`` for the ``aresolve`` path.
    """

    async def run(self, step: Step[Any], context: ResolveContext, inputs: dict[str, Any]) -> Any:
        return await step.aexecute(context, **inputs)


@dataclass(frozen=True)
class BranchRef:
    """Everything a worker needs to re-derive and resolve one fan-out branch.

    The worker imports ``pipeline_module``, finds the ``FanOutStep`` bound to
    ``fan_out_var``, looks up its own ``over[key]`` (so the value never has to
    serialize), calls ``factory(key, value)`` to rebuild the branch sub-DAG (so
    the closure never serializes either), and resolves it. ``branch_hash`` is the
    branch ``StepResult``'s content hash — the cache key the worker writes to and
    the orchestrator polls/loads from. ``params`` are the run's ``InputParam``
    values, which the worker needs to resolve to the same hash.
    """

    pipeline_module: str
    fan_out_var: str
    key: str
    branch_hash: str


class BranchDispatcher(Protocol):
    """Decides *how* a fan-out branch is resolved — in-process or dispatched to a
    worker (e.g. a subprocess) that resolves it and lands the result in the
    shared cache by ``branch.branch_hash``. Carried on ``ResolveContext`` and
    consulted by ``FanOutStep``. The default ``InlineDispatcher`` just resolves
    in-process, so non-fan-out and undistributed graphs behave exactly as before.
    """

    def dispatch(self, branch: BranchRef, sub: StepResult[Any], context: ResolveContext) -> Any: ...


class InlineDispatcher:
    """Resolves each branch in-process — the default. Identical to the pre-dispatch
    behaviour (``FanOutStep`` previously called ``sub.resolve(context)`` directly).
    """

    def dispatch(self, branch: BranchRef, sub: StepResult[Any], context: ResolveContext) -> Any:
        del branch
        return sub.resolve(context)


@dataclass(frozen=True)
class ResolveContext:
    """Execution context passed through the Deferred graph during resolution.

    cache_dir is the on-disk location where step results are read/written for
    cross-run reuse. None means no caching — every resolve recomputes.

    observer is an instrumentation hook (see ``PipelineObserver``). On a bare
    context it defaults to the silent ``NullObserver``; ``Pipeline.execute``
    installs ``LoggingObserver`` (per-step logging) unless a caller passes
    another — e.g. ``NullObserver`` for silence, ``RunTreeObserver``, or the
    inference ``EmbeddingObserver``.

    ``_resolved`` memoizes the resolved result of every ``StepResult`` reached
    during a single ``Pipeline.execute`` call. Without it, a non-cached step
    (``Step`` subclass that returns a non-serializable object — e.g. a model
    model wrapper) would be re-executed once per downstream consumer, and each
    consumer would receive a *different* instance — breaking shared-state
    contracts. With it, the first consumer runs the step, every subsequent
    consumer gets the same object by identity.

    ``_lock`` guards every ``_resolved`` access. The parallel executor resolves a
    whole level in a thread pool, so concurrent nodes mutate this dict; a lock is
    correct regardless of the GIL (the free-threaded 3.13 build removes the
    atomicity the lock-free path used to lean on). Uncontended acquire is ~100 ns,
    negligible against running a step.

    ``_inflight`` is the in-flight registry: while one thread runs a ``StepResult``'s
    body, its ``id`` maps to an ``Event`` other threads wait on instead of re-running
    the same body. It collapses concurrent resolution of one SHARED node to a single
    run (the memo only collapses resolutions that do not overlap in time). The owner
    removes the entry and sets the Event in a ``finally``, so a body that raises
    releases its waiters (which then re-resolve) rather than hanging them. Also
    guarded by ``_lock``.
    """

    cache_dir: Path | None = None
    params: dict[str, Any] = field(default_factory=_empty_params)
    on_progress: Callable[[Progress], None] | None = None
    observer: PipelineObserver = field(default_factory=NullObserver)
    executor: Executor = field(default_factory=LocalExecutor)
    async_executor: AsyncExecutor = field(default_factory=LocalAsyncExecutor)
    dispatcher: BranchDispatcher = field(default_factory=InlineDispatcher)
    _resolved: dict[int, Any] = field(default_factory=_empty_resolved, repr=False, compare=False)
    _inflight: dict[int, Event] = field(default_factory=_empty_inflight, repr=False, compare=False)
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)


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
                # Direct Deferred[ConcreteType] base — auto-detect.
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
        """Async resolution. The default delegates to the synchronous ``resolve`` — correct for LEAF
        Deferreds whose resolution does no awaitable work (``InputParam`` reads ``context.params``,
        ``FileSource`` reads bytes). ``StepResult`` overrides this to await async step bodies and resolve
        upstreams on the event loop, so an existing sync graph runs unchanged on the async path.
        """
        return self.resolve(context)


@dataclass(frozen=True)
class InputParam(Deferred[T]):
    """A named runtime parameter. Use with_default() for optional parameters.

    text = InputParam("text")
    threshold = InputParam.with_default("threshold", 0.5)
    """

    name: str
    default: T | None = None
    has_default: bool = False

    @staticmethod
    def with_default(name: str, default: _S) -> InputParam[_S]:
        """Create a parameter with a default value (even if the default is None)."""
        param: Any = InputParam(name=name, default=default, has_default=True)
        return param

    def resolve(self, context: ResolveContext) -> T:
        if self.name in context.params and context.params[self.name] is not None:
            return context.params[self.name]
        if self.has_default:
            default_value: Any = self.default
            return default_value
        raise ValueError(f"Missing required parameter: {self.name}")
