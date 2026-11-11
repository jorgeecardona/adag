"""Step[Out] and StepResult[Out] — the core of the DAG engine.

Step: base class for all pipeline steps. Configuration lives on frozen
dataclass fields. Inputs flow through __call__() which returns a StepResult.

StepResult: a Deferred that resolves by running a Step. Carries the step
instance and its input sources. On resolve(), checks the disk cache (if the
step has result_type), resolves upstream sources, runs the step, and caches.

    @dataclass(frozen=True)
    class Tokenizer(Step[TokenizedText]):
        result_type = TokenizedText
        max_length: int = 512

        def run(self, text: str) -> TokenizedText:
            ...

    tokenizer = Tokenizer(max_length=256)
    result = tokenizer(text=text_param)   # StepResult
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import time
from abc import abstractmethod
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from threading import Event
from typing import Any, ClassVar, Generic, TypeAlias, TypeVar, get_args, get_origin, get_type_hints
from uuid import UUID

import structlog
from pydantic import BaseModel

from adag.config import Config
from adag.deferred import Deferred, InputParam, Progress, ResolveContext, observing
from adag.hashing import class_source_hash, hash_scalar, is_dict, is_list, is_tuple
from adag.io import FileSource, atomic_write_text

logger = structlog.get_logger()

Out = TypeVar("Out")


def _types_compatible(expected: Any, actual: Any) -> bool:
    """True when an ``actual`` Deferred output type is assignable to an ``expected`` slot.

    Liskov-substitution-aware: a concrete subclass is accepted where the
    parent type is declared. Exact equality is the fallback for parameterized
    generics (``list[Foo]``, ``dict[K, V]``) where ``issubclass`` doesn't apply.
    ``Optional[T]`` / ``T | None`` is unwrapped so a non-optional ``T``
    satisfies an optional slot.

    Used by ``StepResult._validate`` so siblings *and* subclasses of a
    declared input type can be wired without dropping to ``Any`` + runtime
    isinstance checks at the call site.
    """
    if expected is Any or actual is Any:
        return True
    if isinstance(expected, type) and isinstance(actual, type):
        return issubclass(actual, expected)
    if expected == actual:
        return True
    expected_args = [arg for arg in get_args(expected) if arg is not type(None)]
    if len(expected_args) == 1:
        return _types_compatible(expected_args[0], actual)
    return False


def _serialize_step_field(value: Any) -> tuple[bool, Any]:
    """Convert a Step's dataclass field to a JSON-serializable form for step.json.

    Returns ``(True, json_value)`` for fields that belong in the step's
    persisted spec, or ``(False, None)`` for fields that are runtime state
    (Step instances pointing at upstream deps, tokenizers, HTTP clients,
    weights — anything owned by ``save_artifacts`` overrides on the
    subclass).

    Supported config types: scalars, ``None``, ``Path``, ``UUID``, ``Enum``,
    ``BaseModel`` (via ``model_dump(mode="json")``), and list/tuple/dict
    composites of those.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return True, value
    if isinstance(value, (Path, UUID)):
        return True, str(value)
    if isinstance(value, Enum):
        return True, value.value
    if isinstance(value, BaseModel):
        return True, value.model_dump(mode="json")
    if is_list(value) or is_tuple(value):
        return _serialize_sequence_field(value)
    if is_dict(value):
        return _serialize_dict_field(value)
    return False, None


def _serialize_sequence_field(value: list[Any] | tuple[Any, ...]) -> tuple[bool, Any]:
    """Recursive helper for list/tuple inside ``_serialize_step_field``."""
    serialized: list[Any] = []
    for item in value:
        handled, item_value = _serialize_step_field(item)
        if not handled:
            return False, None
        serialized.append(item_value)
    return True, serialized


def _serialize_dict_field(value: dict[str, Any]) -> tuple[bool, Any]:
    """Recursive helper for dict inside ``_serialize_step_field``."""
    serialized: dict[str, Any] = {}
    for key, item in value.items():
        handled, item_value = _serialize_step_field(item)
        if not handled:
            return False, None
        serialized[str(key)] = item_value
    return True, serialized


class StepTimeout(TimeoutError):
    """Raised when a step's async body exceeds its ``timeout_s`` budget. Subclasses the builtin
    ``TimeoutError`` so callers can catch either.
    """


@dataclasses.dataclass(frozen=True, eq=False)
class Step(Generic[Out]):
    """A step in the DAG — a frozen dataclass; subclasses add config fields and run().

    Frozen so a step's identity (its ``content_hash`` / cache key) can't change after
    construction. The base sets ``eq=False`` (identity equality), which steps like
    ``FanOutStep`` rely on for distinct-instance semantics; a subclass that wants
    field-based equality just declares ``@dataclass(frozen=True)`` (eq=True by default).
    Being a dataclass lets the engine read ``dataclasses.fields(self)`` directly.

    Class attributes:
        kind: step identifier for serialization (e.g. "adag/tokenizer/v1").
        label / description: human-readable text for visualization.
        result_type: set to a ``BaseModel`` subclass to enable disk caching.
        cache_key: pinned cache key that overrides the computed content hash.
        pure: whether the body is a deterministic function of its declared inputs.
        timeout_s: wall-clock budget (seconds) for the async resolve path; ``None`` = unbounded.

    ``pure`` is PROVENANCE only — it is orthogonal to caching and never gates it. An impure step
    (an LLM draw, a clock/UUID read, an external service) is still cached by input-hash to freeze
    one result for cost and reproducibility; ``pure`` only records whether re-running would
    reproduce that value, so a consumer can tell a never-stale pure cache from a deliberately
    refreshable generative one. A seeded RNG counts as pure; an unseeded one does not.

    ``timeout_s`` bounds the awaitable body on the async path only: ``aresolve`` wraps it in
    ``asyncio.wait_for`` and raises ``StepTimeout`` on overrun, so a stuck step fails as a clean
    node error instead of hanging the host. It cannot interrupt synchronous CPU offloaded to a
    worker thread — run that through a killable ``SubprocessDispatcher`` if you need a hard limit.
    """

    kind: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    result_type: ClassVar[type[BaseModel] | None] = None
    return_type: ClassVar[type[Any] | None] = None
    cache_key: ClassVar[str | None] = None
    strict_content_hash: ClassVar[bool] = False
    pure: ClassVar[bool] = True
    timeout_s: ClassVar[float | None] = None
    # Optional typed ports for connection-validity introspection. `produces` is this step's output port-type;
    # `accepts` is the set of upstream port-types it can consume. Both default to None (untyped → unconstrained),
    # so existing steps declare no port and `accepts_from` never rejects them.
    produces: ClassVar[str | None] = None
    accepts: ClassVar[tuple[str, ...] | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for parent in getattr(cls, "__orig_bases__", []):
            origin = get_origin(parent)
            # Accept both `Step[X]` and any subclass of Step that's also generic
            # (e.g. `SerializableStep[X]`). Without this, `SerializableStep`
            # subclasses never get their return_type / result_type inferred.
            if origin is not None and isinstance(origin, type) and issubclass(origin, Step):
                args = get_args(parent)
                if args and isinstance(args[0], type):
                    if "return_type" not in cls.__dict__:
                        cls.return_type = args[0]
                    if "result_type" not in cls.__dict__ and issubclass(args[0], BaseModel):
                        cls.result_type = args[0]
                return

    def result_model(self) -> type[BaseModel] | None:
        """The concrete pydantic model the result is cached as (JSON round-trip). Defaults to the class
        ``result_type`` inferred from ``Step[X]``; override when the result type is generic (e.g. ``Dataset[T]``)
        so the parametrized model is known at cache-load time, not the unparametrized base.
        """
        return type(self).result_type

    def output_type(self) -> type[Any] | None:
        """The deferred output type used for wiring / connection checks. Defaults to the class ``return_type``;
        override alongside ``result_model`` when the result type is generic.
        """
        return type(self).return_type

    def __call__(self, **kwargs: Any) -> StepResult[Out]:
        """Wire this step into the DAG. Returns a StepResult that chains downstream."""
        return StepResult(step=self, sources=kwargs)

    def accepts_from(self, producer: Step[Any] | str) -> bool:
        """Typed-PORT connection validity: can THIS step consume ``producer``'s output? (`producer` may be a
        Step — its ``produces`` is read — or a bare produces-string.) UNTYPED on either side (no port declared)
        ⇒ True, so existing pipelines are unconstrained and unaffected; otherwise ``producer.produces`` must be
        in this step's ``accepts`` set. The type-compatibility gate ("only type-compatible connections can be
        made"); acyclicity is enforced separately by Pipeline. Additive — ports default untyped.
        """
        produces = producer if isinstance(producer, str) else producer.produces
        if produces is None or self.accepts is None:
            return True
        return produces in self.accepts

    def run(self, **kwargs: Any) -> Out:
        """Execute the step's synchronous logic; receives resolved inputs as kwargs.

        Override in a synchronous step. Deliberately NOT ``@abstractmethod``: an
        ``AsyncStep`` (implements ``arun``) and a ``FanOutStep`` (driven via ``execute``)
        have no synchronous ``run``, and marking it abstract would make them
        un-instantiable. The default raise instead guards the real misuse cases — a sync
        step that forgot ``run``, or a sync resolve of an async step.
        """
        del kwargs
        raise NotImplementedError(
            f"{type(self).__name__} has no run(): a synchronous Step must implement run(); "
            "an AsyncStep implements arun() and must be resolved via Pipeline.aexecute()."
        )

    def execute(self, context: ResolveContext, /, **kwargs: Any) -> Out:
        """Framework entry point. Override if the step needs per-call state
        (e.g. BatchStep needs context.cache_dir for its ItemCache). Most
        steps don't — the default binds the run's observer as the ambient
        ``current_observer()`` (so step internals, incl. worker threads and
        asyncio tasks, can emit observer hooks) and delegates to run().
        """
        with observing(context.observer):
            return self.run(**kwargs)

    async def arun(self, **kwargs: Any) -> Out:
        """Async step body. Default delegates to the sync ``run`` so a sync step works unchanged on the
        async path; a step that awaits I/O subclasses ``AsyncStep`` and overrides this.
        """
        return self.run(**kwargs)

    async def aexecute(self, context: ResolveContext, /, **kwargs: Any) -> Out:
        """Async framework entry point — the awaitable mirror of ``execute``. Override for per-call state;
        the default delegates to ``arun``.
        """
        del context
        return await self.arun(**kwargs)

    def content_hash(self) -> str | None:
        """Compute a stable content hash identifying this step's configuration.

        Identity:
          - If `kind` is set (SerializableStep subclasses), the kind string is the identity.
          - Otherwise, the class name + class source hash — so code changes invalidate the cache.

        Fields:
          - `Config` — skipped (runtime-only values, not identity).
          - `FileSource` — hashed by file content; a missing file blocks the hash (`None`).
          - `BaseModel` — hashed by pydantic JSON dump.
          - Nested `Step` — recursed; a blocked nested hash blocks the parent.
          - Scalars and `list` — hashed directly.
          - Anything else — raises if `strict_content_hash` is set, skipped otherwise.

        Subclasses only override this if they need a fundamentally different identity strategy.
        """
        pinned = type(self).cache_key
        if pinned is not None:
            return pinned

        h = hashlib.sha256()
        kind = type(self).kind
        if kind:
            h.update(b"KIND:" + kind.encode())
        else:
            h.update(b"STEP:" + type(self).__name__.encode())
            code_bytes = class_source_hash(type(self))
            if code_bytes:
                h.update(b"CODE:" + code_bytes)

        for field in dataclasses.fields(self):
            if field.name == "node_label":
                continue  # display-only label for the graph renderer (visualization.walk), not compute identity
            chunk = self._hash_field(field.name, getattr(self, field.name))
            if chunk is None:
                return None
            h.update(chunk)

        return h.hexdigest()[:12]

    def _hash_field(self, field_name: str, value: Any) -> bytes | None:
        """Hash a single dataclass field for content_hash.

        Returns:
          - `bytes` to mix into the parent hash (empty bytes = skip).
          - `None` to block the parent hash entirely (unsatisfied input).

        Unknown field types are silently skipped by default; set
        `strict_content_hash = True` on the class to raise instead.
        """
        if isinstance(value, Config):
            return b""
        if isinstance(value, FileSource):
            file_content_hash = value.content_hash()
            if file_content_hash is None:
                return None
            return b"FILE:" + field_name.encode() + file_content_hash.encode()
        if isinstance(value, BaseModel):
            dumped = json.dumps(value.model_dump(mode="json"), sort_keys=True, default=str).encode()
            return b"PYD:" + field_name.encode() + dumped
        if isinstance(value, Step):
            sub = value.content_hash()
            if sub is None:
                return None
            return b"SUB:" + field_name.encode() + sub.encode()
        return self._hash_plain_field(field_name, value)

    def _hash_plain_field(self, field_name: str, value: Any) -> bytes | None:
        """Hash a non-wrapper field: scalar, class object, or list/tuple (the tail of ``_hash_field``)."""
        if isinstance(value, (str, int, float, bool)) or value is None:
            return b"SCALAR:" + field_name.encode() + hash_scalar(value)
        # A class object (e.g. a generic step's item/example type) is identity — hash it by qualified name so two
        # parametrizations do not collide on one cache key. Without this a `type`-valued field would be skipped.
        if isinstance(value, type):
            return (
                b"TYPE:" + field_name.encode() + f"{value.__module__}.{value.__qualname__}".encode()
            )
        # Tuples hash like lists — useful for fixed-size config (categories,
        # axes, etc.) where the dataclass author wants immutability.
        if is_list(value) or is_tuple(value):
            return self._hash_list(field_name, list(value))
        if type(self).strict_content_hash:
            raise TypeError(
                f"Field '{field_name}' has unsupported type {type(value).__name__}. "
                f"Allowed: str, int, float, bool, type, list, tuple, FileSource, Step, BaseModel, Config."
            )
        return b""

    def _hash_list(self, field_name: str, value: list[Any]) -> bytes:
        """Hash a list field, handling mixed scalar and BaseModel items.

        Nested tuples/lists are recursively hashed so configs like
        ``list[tuple[str, int]]`` (e.g. ordered cap mappings) work without
        forcing the caller to flatten or wrap them in a BaseModel.
        """
        h = hashlib.sha256()
        h.update(b"LIST:" + field_name.encode())
        for item in value:
            if isinstance(item, BaseModel):
                h.update(
                    json.dumps(item.model_dump(mode="json"), sort_keys=True, default=str).encode()
                )
            elif isinstance(item, (str, int, float, bool)) or item is None:
                h.update(hash_scalar(item))
            elif is_list(item) or is_tuple(item):
                h.update(self._hash_list(f"{field_name}[]", list(item)))
            elif type(self).strict_content_hash:
                raise TypeError(
                    f"List field '{field_name}' has unsupported item type {type(item).__name__}. "
                    f"Allowed items: str, int, float, bool, list, tuple, BaseModel."
                )
        return h.digest()

    def save_artifacts(self, path: str) -> None:
        """Write step.json with the step's kind and serialized config fields.

        Default implementation captures the dataclass fields that ``_serialize_step_field``
        recognises (scalars, Path, UUID, Enum, BaseModel, composites thereof).
        Fields representing runtime state (Step instances, tokenizers, HF clients,
        model handles) are skipped — those subclasses override this method, call
        ``super().save_artifacts(path)``, then write their own state files
        (safetensors, tokenizer files, etc.) next to step.json.

        SerializableStep overrides to keep the legacy ``data["spec"] = model_dump``
        format that ``load_spec()`` expects; the formats stay distinct because
        flattening would break round-trip for every existing saved pipeline.
        """
        os.makedirs(path, exist_ok=True)
        step_config: dict[str, Any] = {}
        for field in dataclasses.fields(self):
            handled, json_value = _serialize_step_field(getattr(self, field.name))
            if handled:
                step_config[field.name] = json_value

        kind = getattr(self, "kind", None) or type(self).__name__.lower()
        step_json: dict[str, Any] = {"kind": kind, "spec": step_config}
        atomic_write_text(Path(path).joinpath("step.json"), json.dumps(step_json, indent=2) + "\n")

    def load_artifacts(self, path: str) -> None:
        """Load step-specific artifacts from a directory.

        Override in steps that carry heavy state (neural network modules, etc.).
        """
        del path


class AsyncStep(Step[Out]):
    """A step whose body AWAITS I/O — a DB session, an HTTP/LLM call. Implement ``arun`` (async) instead of
    ``run`` (sync), and resolve it through ``Pipeline.aexecute`` / ``StepResult.aresolve``.

    An AsyncStep has no synchronous body: it does not override ``run``, so the synchronous path
    (``execute``/``resolve``) hits ``Step.run``'s default, which raises a clear message — a sync
    resolve cannot await an async body. Sync steps are unaffected: their inherited ``arun`` just
    runs ``run``, so a graph can freely MIX sync and async steps and resolve the whole thing with
    ``aexecute`` (each sync step runs inline, each async step is awaited).

    Per-request resources (e.g. a SQLAlchemy ``AsyncSession``) flow in like any other input — wire an
    ``InputParam`` and pass it to ``aexecute``; nothing special is needed on the context.
    """

    @abstractmethod
    async def arun(self, **kwargs: Any) -> Out:
        """Execute the async step logic. Receives resolved input values as kwargs."""
        ...


class _Miss:
    """Sentinel for a cache/memo miss — deliberately not ``None``, because a step may
    legitimately return ``None`` and that must not read as 'recompute me'.
    """


_CACHE_MISS = _Miss()


class StepResult(Deferred[Out]):
    """Deferred result of a Step. Handles caching, upstream resolution, and type validation."""

    def __init__(self, step: Step[Out], sources: dict[str, _SourceValue]) -> None:
        self.step = step
        self.sources: dict[str, _SourceValue] = sources
        self.deferred_type = step.output_type()
        # Memoize content_hash(): it recurses into every upstream and is called many times
        # per node (resolve, is_cached, load), so a deep chain would recompute hashes
        # ~quadratically without this. Single slot, keyed on the params object's IDENTITY
        # (graph + params are immutable within one execute(); a new params object recomputes).
        # _hash_computed separates "not computed yet" from "computed as None" (a blocked
        # step hashes to None), so params=None still computes exactly once.
        self._hash_computed: bool = False
        self._hash_params: dict[str, Any] | None = None
        self._hash_value: str | None = None
        self._validate()

    def _validate(self) -> None:
        """Runtime type check at wiring time. Best-effort — skips if hints fail. An ``AsyncStep``'s input
        types live on ``arun`` (it has no synchronous ``run``), so validate against the step's real body.
        """
        target = type(self.step).arun if isinstance(self.step, AsyncStep) else type(self.step).run
        try:
            run_hints = get_type_hints(target)
        except Exception as exc:
            # Best-effort: a forward ref that can't resolve (missing import, stringized
            # annotation) means we can't type-check the wiring — log it rather than fail
            # silently, then skip (wiring still works; this is a guard, not a gate).
            logger.debug(
                "step.validate.hints_unavailable", step=type(self.step).__name__, error=str(exc)
            )
            return
        for name, source in self.sources.items():
            if isinstance(source, Deferred):
                expected: Any = run_hints.get(name)
                actual: Any = source.deferred_type
                if expected and actual and not _types_compatible(expected, actual):
                    expected_name = getattr(expected, "__name__", repr(expected))
                    actual_name = getattr(actual, "__name__", repr(actual))
                    raise TypeError(
                        f"Type mismatch: {type(source).__name__} produces {actual_name}, "
                        f"but {type(self.step).__name__} expects {expected_name} for '{name}'"
                    )

    def _check_cache(self, context: ResolveContext) -> Out | _Miss:
        """The shared, synchronous *front* of ``resolve``/``aresolve``: per-execute memo
        lookup, ``on_step_start``, and the disk-cache hit path. Returns the resolved value
        on a memo/cache hit, or ``_CACHE_MISS`` if the step still has to run.

        Disk IO is synchronous on both paths — it is not the bottleneck and keeps the cache
        format byte-identical. ``context._lock`` guards the memo dict so the parallel
        executor's concurrent nodes are safe regardless of the GIL.
        """
        # Per-execute memoization. id() is safe because StepResult instances live for the
        # lifetime of the Pipeline.execute call.
        with context._lock:
            if id(self) in context._resolved:
                return context._resolved[id(self)]

        step_name = type(self.step).__name__
        # Announce entry BEFORE the recursive hash/cache check, so progress shows the instant
        # a step is reached. Firing on_step_start before source resolution also brackets every
        # child's start/finish inside this step's — that nesting is what the RunTreeObserver
        # turns back into the execution tree.
        context.observer.on_step_start(step_name)

        if (
            context.cache_dir is not None
            and self.step.result_model() is not None
            and self.is_cached(context.cache_dir, params=context.params)
        ):
            if context.on_progress:
                context.on_progress(Progress(step=step_name, message="cache hit"))
            loaded = self.load(context.cache_dir, params=context.params)
            with context._lock:
                context._resolved[id(self)] = loaded
            context.observer.on_step_cached(step_name)
            return loaded

        if context.on_progress:
            context.on_progress(Progress(step=step_name, message="resolving"))
        return _CACHE_MISS

    def _finalize(self, result: Out, context: ResolveContext, started_at: float) -> Out:
        """The shared, synchronous *back* of ``resolve``/``aresolve``: observer-finish,
        disk-cache write, and the memo write. Same on both paths.
        """
        step_name = type(self.step).__name__
        context.observer.on_step_finished(step_name, time.perf_counter() - started_at)
        if context.cache_dir is not None and self.step.result_model() is not None:
            self._save(result, context.cache_dir, params=context.params)
            logger.info(
                "Cached to disk", step=step_name, key=self.content_hash(params=context.params)
            )
        with context._lock:
            context._resolved[id(self)] = result
        return result

    def resolve(self, context: ResolveContext) -> Out:
        """Resolve upstream sources, run the step, cache if applicable.

        Memoizes by ``id(self)`` so multiple downstream consumers of the SAME ``StepResult``
        share one computed result within a ``Pipeline.execute`` — critical for non-cached
        steps returning non-serializable objects (e.g. model handles), where consumers must
        see the same instance, not two re-executions. The cache/memo/observer skeleton lives
        in ``_check_cache``/``_finalize`` (shared with ``aresolve``); only the two lines that
        genuinely differ — upstream resolution and the step body — stay here, run inline.

        In-flight dedup makes that guarantee hold under CONCURRENCY too: the memo only
        collapses resolutions that do not overlap in time, so N branch workers reaching one
        shared upstream at once would each miss the memo and run the body N times. Here the
        first thread to miss the memo claims ownership (an ``Event`` in ``context._inflight``);
        concurrent threads find that Event, wait on it, then loop — a success left the value in
        the memo (returned), a failure removed the marker so they re-claim and resolve it
        themselves. Uncontended (single-threaded) there is never a waiter, so behaviour and
        cost are unchanged bar creating one Event that is set immediately.
        """
        while True:
            with context._lock:
                if id(self) in context._resolved:
                    return context._resolved[id(self)]
                in_flight = context._inflight.get(id(self))
                if in_flight is None:
                    own_event = Event()
                    context._inflight[id(self)] = own_event
                    break
            # Another thread owns this exact node's resolution. Wait for it, then loop.
            in_flight.wait()
        try:
            cached = self._check_cache(context)
            if not isinstance(cached, _Miss):
                return cached
            resolved = self._merge_step_defaults(self._resolve_sources(context))
            started_at = time.perf_counter()
            result = context.executor.run(self.step, context, resolved)
            return self._finalize(result, context, started_at)
        finally:
            with context._lock:
                context._inflight.pop(id(self), None)
            own_event.set()

    def _merge_step_defaults(self, resolved: dict[str, Any]) -> dict[str, Any]:
        """Merge the step's own (non-source) dataclass field values into the
        resolved sources, unwrapping Config fields so run() receives the
        underlying value. Resolved sources win over default field values.
        """
        fields: dict[str, Any] = {}
        for f in dataclasses.fields(self.step):
            if f.name in resolved:
                continue
            value: Any = getattr(self.step, f.name)
            if isinstance(value, Config):
                value = value.value
            fields[f.name] = value
        return {**fields, **resolved}

    def _resolve_sources(self, context: ResolveContext) -> dict[str, Any]:
        """Resolve all upstream sources."""
        resolved: dict[str, Any] = {}
        for name, source in self.sources.items():
            if isinstance(source, Config):
                resolved[name] = source.value
            elif isinstance(source, Deferred):
                resolved[name] = source.resolve(context)
            else:
                resolved[name] = source
        return resolved

    async def aresolve(self, context: ResolveContext) -> Out:
        """Async mirror of ``resolve`` — same ``_check_cache``/``_finalize`` skeleton, but the two
        genuinely-different lines ``await``: upstream resolution (``_aresolve_sources``) and the step
        body (``context.async_executor``). So async steps run ON the event loop, and a cancelled
        request unwinds through the awaits without starting further steps.
        """
        cached = self._check_cache(context)
        if not isinstance(cached, _Miss):
            return cached
        resolved = self._merge_step_defaults(await self._aresolve_sources(context))
        started_at = time.perf_counter()
        timeout = self.step.timeout_s
        body = context.async_executor.run(self.step, context, resolved)
        if timeout is None:
            result = await body
        else:
            try:
                result = await asyncio.wait_for(body, timeout)
            except TimeoutError as e:  # asyncio.TimeoutError is TimeoutError on 3.11+
                raise StepTimeout(
                    f"step {type(self.step).__name__} (kind={self.step.kind or '-'}, label={self.step.label or '-'}) "
                    f"exceeded timeout_s={timeout}"
                ) from e
        return self._finalize(result, context, started_at)

    async def _aresolve_sources(self, context: ResolveContext) -> dict[str, Any]:
        """Resolve all upstream sources on the async path, one at a time.

        Sequential by design: a single shared
        per-request resource (e.g. one ``AsyncSession``) cannot run concurrent operations, and the
        ``context._resolved`` memo already collapses shared upstreams to one resolution. (Concurrency
        across independent siblings is a possible future opt-in, not the safe default.)
        """
        resolved: dict[str, Any] = {}
        for name, source in self.sources.items():
            if isinstance(source, Config):
                resolved[name] = source.value
            elif isinstance(source, Deferred):
                resolved[name] = await source.aresolve(context)
            else:
                resolved[name] = source
        return resolved

    def content_hash(self, params: dict[str, Any] | None = None) -> str | None:
        """Content hash: step identity + source hashes (memoized per params identity)."""
        if self._hash_computed and params is self._hash_params:
            return self._hash_value
        result = self._compute_content_hash(params)
        self._hash_computed = True
        self._hash_params = params
        self._hash_value = result
        return result

    def _compute_content_hash(self, params: dict[str, Any] | None = None) -> str | None:
        step_hash = self.step.content_hash()
        if step_hash is None:
            return None

        h = hashlib.sha256()
        h.update(step_hash.encode())

        for name, source in sorted(self.sources.items()):
            h.update(name.encode())
            if isinstance(source, StepResult):
                upstream_hash = source.content_hash(params=params)
                if upstream_hash is None:
                    return None
                h.update(upstream_hash.encode())
            elif isinstance(source, InputParam):
                if params and source.name in params:
                    param_value: Any = params[source.name]
                elif source.has_default:
                    param_value = source.default
                else:
                    param_value = None
                h.update(f"param:{source.name}:{param_value!r}".encode())
            elif isinstance(source, Deferred):
                # Use the Deferred's content_hash if it implements one
                # (FileSource hashes by file bytes, etc.). Falling back to
                # just the class name would silently mask content changes:
                # editing a FileSource-tracked file would not invalidate any
                # downstream cache. This is the path that surfaces file-byte
                # changes through the DAG.
                deferred_hash = source.content_hash() if hasattr(source, "content_hash") else None
                if deferred_hash is None:
                    h.update(f"deferred:{type(source).__name__}".encode())
                else:
                    h.update(f"deferred:{type(source).__name__}:{deferred_hash}".encode())
            else:
                h.update(repr(source).encode())

        return h.hexdigest()[:12]

    def _output_path(self, cache_dir: Path, params: dict[str, Any] | None = None) -> Path | None:
        content_hash = self.content_hash(params=params)
        if content_hash is None:
            return None
        return cache_dir.joinpath(content_hash)

    def _done_path(self, cache_dir: Path, params: dict[str, Any] | None = None) -> Path | None:
        content_hash = self.content_hash(params=params)
        if content_hash is None:
            return None
        return cache_dir.joinpath(content_hash + ".done")

    def is_cached(self, cache_dir: Path, params: dict[str, Any] | None = None) -> bool:
        output = self._output_path(cache_dir, params=params)
        done = self._done_path(cache_dir, params=params)
        if output is None or done is None:
            return False
        if not output.exists() or not done.exists():
            return False
        return self._validate_cached(output)

    def _validate_cached(self, path: Path) -> bool:
        """Check if cached output is valid by parsing with the result model."""
        result_type = self.step.result_model()
        if result_type is None:
            return False
        try:
            content = path.read_text()
            if not content.strip():
                return True
            result_type.model_validate_json(content)
            return True
        except Exception as exc:
            # A corrupt / schema-drifted cache entry is treated as a miss (never crash on a
            # bad cache file), but log it so a systematically-broken cache surfaces.
            logger.debug(
                "step.cache.invalid_entry",
                step=type(self.step).__name__,
                path=str(path),
                error=str(exc),
            )
            return False

    def _save(self, result: Out, cache_dir: Path, params: dict[str, Any] | None = None) -> None:
        output = self._output_path(cache_dir, params=params)
        done = self._done_path(cache_dir, params=params)
        if output is None or done is None:
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(result, BaseModel):
            new_text = result.model_dump_json(indent=2)
            # Invariant: same content_hash -> same bytes. Overwriting an existing hash with
            # different bytes means the cache key is missing an input; log loudly (the message
            # below has the likely causes) rather than silently ship stale results.
            if output.exists():
                try:
                    old_text = output.read_text()
                except OSError:
                    old_text = None
                if old_text is not None and old_text != new_text:
                    logger.error(
                        "cached_step.invariant_violation",
                        step=type(self.step).__name__,
                        hash=output.stem,
                        message=(
                            "Same content_hash produced different bytes. The "
                            "step's cache key is missing an input — likely a "
                            "side-channel file (model weights, embeddings) or "
                            "a sub-batch whose item keys do not encode all "
                            "inputs. Overwriting cache with new bytes."
                        ),
                    )
            atomic_write_text(output, new_text)
        else:
            raise TypeError(
                f"Cannot cache {type(result).__name__}: result_type is set but result is not a BaseModel"
            )
        atomic_write_text(done, datetime.now(UTC).isoformat())

    def load(self, cache_dir: Path, params: dict[str, Any] | None = None) -> Out:
        output = self._output_path(cache_dir, params=params)
        result_type = self.step.result_model()
        if output is None or result_type is None or not output.exists():
            raise ValueError(f"Output for '{type(self.step).__name__}' not found.")
        return result_type.model_validate_json(output.read_text())  # pyright: ignore[reportReturnType]

    def __repr__(self) -> str:
        return f"StepResult({type(self.step).__name__})"


# Defined after StepResult so the self-reference resolves. With
# from __future__ import annotations, all annotations in this module
# are lazy strings — _SourceValue is looked up at type-check time.
_SourceValue: TypeAlias = (
    StepResult[Any] | InputParam[Any] | FileSource | Config[Any] | str | int | float | bool | None
)
