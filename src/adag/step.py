"""Step and StepResult: the two core types of the graph.

``Step`` is the base class of every step. Its dataclass fields are configuration. Calling a
step wires its inputs and returns a ``StepResult``.

``StepResult`` is one node of the graph. Resolving it loads the cached result when there is
one. Otherwise it resolves the inputs, runs the step and saves the result.

    @dataclass(frozen=True)
    class Tokenizer(Step[TokenizedText]):
        max_length: int = 512

        def run(self, text: str, max_length: int) -> TokenizedText: ...

    result = Tokenizer(max_length=256)(text=text_param)  # a StepResult
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import shutil
import time
import warnings
from abc import abstractmethod
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from threading import Event
from typing import Any, ClassVar, Generic, TypeAlias, TypeVar, get_args, get_origin, get_type_hints
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel

from adag.cache_store import CacheStore, FileSystemCacheStore
from adag.config import Config
from adag.deferred import Deferred, InputParam, Progress, ResolveContext, emit, observing
from adag.deprecation import AdagDeprecationWarning
from adag.hashing import (
    KEY_FORMAT,
    UnhashableValueError,
    class_source_hash,
    encode_value,
    is_dict,
    is_list,
    is_tuple,
)
from adag.io import FileSource, atomic_write_text
from adag.key import ComponentKind, Expansion, KeyComponent, KeyExplanation

logger = structlog.get_logger()

# Step classes already warned about a save_result/load_result round trip that loses data.
_ROUND_TRIP_WARNED: set[type] = set()


def _round_trip_differs(produced: object, reloaded: object) -> bool:
    """Return True when ``load_result`` gave back something other than what ``run`` produced.

    A ``Path`` result is skipped: it is expected to change to the final cache path. A pydantic
    model is compared field by field, including fields excluded from serialization, because
    those are the ones a round trip drops silently.
    """
    if isinstance(produced, Path):
        return False
    try:
        if isinstance(produced, BaseModel) and isinstance(reloaded, BaseModel):
            return dict(produced) != dict(reloaded)
        return bool(produced != reloaded)
    except Exception:
        return False


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
    weights: anything owned by ``save_artifacts`` overrides on the
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


def _overrides(cls: type, name: str) -> bool:
    """Return whether ``cls`` replaces ``Step``'s method ``name``."""
    own: object = getattr(cls, name)
    base: object = getattr(Step, name)
    return own is not base


@dataclasses.dataclass(frozen=True, eq=False)
class Step(Generic[Out]):
    """Base class of every step: a frozen dataclass with config fields and a ``run()`` method.

    Fields are part of the cache key, except ``Config`` fields. adag passes every field to
    ``run()`` by name, together with the wired inputs.

    Class attributes:
        kind: a stable name for the step. It replaces the class name in the key and in
            ``step.json``.
        label, description: text for the graph drawing.
        result_type: the ``BaseModel`` the result is cached as. Inferred from ``Step[X]``.
        cache_key: a fixed key that replaces the computed hash.
        strict_content_hash: set to ``False`` to skip fields the key cannot encode.
        pure: ``False`` records that a rerun would not give the same value (a model call, a
            clock read). adag still caches the step and never reads ``pure``.
        timeout_s: seconds allowed for the async body under ``aexecute`` or ``aresolve``. A longer
            run raises ``StepTimeout``. ``execute()`` ignores it, and it cannot stop a blocking
            sync body. Use a ``SubprocessDispatcher`` when you need a hard limit.
        produces, accepts: optional port names for graph editors. See ``accepts_from``.

    Equality is by identity (``eq=False``), which ``FanOutStep`` relies on. A subclass declared
    with ``@dataclass(frozen=True)`` compares by fields.
    """

    kind: ClassVar[str] = ""
    label: ClassVar[str] = ""
    description: ClassVar[str] = ""
    result_type: ClassVar[type[BaseModel] | None] = None
    return_type: ClassVar[type[Any] | None] = None
    cache_key: ClassVar[str | None] = None
    strict_content_hash: ClassVar[bool] = True
    pure: ClassVar[bool] = True
    timeout_s: ClassVar[float | None] = None

    # Optional typed ports, read by graph editors to check a connection before it is made.
    # ``produces`` is this step's output port type; ``accepts`` the port types it can consume.
    # ``None`` means untyped: ``accepts_from`` never rejects it. ``Pipeline`` does not check them.
    produces: ClassVar[str | None] = None
    accepts: ClassVar[tuple[str, ...] | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        overrides_save = _overrides(cls, "save_result")
        overrides_load = _overrides(cls, "load_result")
        if overrides_save != overrides_load:
            raise TypeError(
                f"{cls.__name__} overrides only one of save_result/load_result; override both, "
                "so a cached result can be read back."
            )
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

    def save_result(self, result: Out, path: Path) -> None:
        """Write ``result`` to ``path``. Override with ``load_result`` for a non-JSON result.

        ``path`` does not exist yet. Create it as a file or as a directory (a directory holds a
        result plus any sidecar files). adag passes a temporary path and renames it into place
        when this returns, so a crash never leaves a half-written result. The default writes the
        ``BaseModel`` result as JSON.
        """
        if not isinstance(result, BaseModel):
            raise TypeError(
                f"{type(self).__name__}: the default save_result writes a BaseModel; got "
                f"{type(result).__name__}. Override save_result and load_result."
            )
        atomic_write_text(path, result.model_dump_json(indent=2))

    def load_result(self, path: Path) -> Out:
        """Read the result that ``save_result`` wrote at ``path``.

        Raise any exception when the entry is not valid: adag treats it as a cache miss and runs
        the step again. The default reads JSON with ``result_model()``.
        """
        model = self.result_model()
        if model is None:
            raise TypeError(f"{type(self).__name__} has no BaseModel result type to load")
        return model.model_validate_json(path.read_text())  # pyright: ignore[reportReturnType]

    @classmethod
    def caches_to_path(cls) -> bool:
        """Return whether this step overrides ``save_result``/``load_result``.

        Such a step is cached under ``cache_dir`` as a file or directory, never in a ``store``.
        """
        return _overrides(cls, "save_result")

    def accepts_from(self, producer: Step[Any] | str) -> bool:
        """Return whether this step can consume ``producer``'s output port.

        ``producer`` is a step (its ``produces`` is read) or a port name. Returns ``True`` when
        either side declares no port; otherwise ``producer.produces`` must be in ``accepts``.
        """
        produces = producer if isinstance(producer, str) else producer.produces
        if produces is None or self.accepts is None:
            return True
        return produces in self.accepts

    def __call__(self, **kwargs: Any) -> StepResult[Out]:
        """Wire this step into the DAG. Returns a StepResult that chains downstream.

        Warns ``AdagDeprecationWarning`` when an input has the name of a field that holds a ``Step``:
        the field copy is hashed but never run, and the input value is what ``run()`` gets. The
        first release documented this pattern, so it still works. It becomes a ``TypeError`` in
        adag 1.0. Wire the upstream step once, as an input.

        Raises:
            TypeError: a field holds a ``StepResult``. A field is part of the key, not an edge;
                pass the upstream node as a call input instead.
        """
        for f in dataclasses.fields(self):
            if isinstance(getattr(self, f.name), StepResult):
                raise TypeError(
                    f"{type(self).__name__}.{f.name} holds a StepResult. A field is hashed into "
                    f"the cache key, not wired as an edge: pass it as a call input, "
                    f"{type(self).__name__}(...)({f.name}=node)."
                )
            if f.name in kwargs and isinstance(getattr(self, f.name), Step):
                warnings.warn(
                    f"{type(self).__name__}.{f.name} is both a Step field and an input. "
                    f"Remove the field and pass the upstream result as {f.name}=... only. "
                    "This becomes a TypeError in adag 1.0.",
                    AdagDeprecationWarning,
                    stacklevel=2,
                )
        return StepResult(step=self, sources=kwargs)

    def run(self, **kwargs: Any) -> Out:
        """Execute the step's synchronous logic; receives resolved inputs as kwargs.

        Override in a synchronous step. Deliberately NOT ``@abstractmethod``: an
        ``AsyncStep`` (implements ``arun``) and a ``FanOutStep`` (driven via ``execute``)
        have no synchronous ``run``, and marking it abstract would make them
        un-instantiable. The default raise instead guards the real misuse cases, a sync
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
        steps don't: the default binds the run's observer as the ambient
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
        """Async framework entry point: the awaitable mirror of ``execute``. Override for per-call state;
        the default delegates to ``arun``.
        """
        del context
        return await self.arun(**kwargs)

    def expansion(self) -> Expansion | None:
        """Return how this step builds its sub-graph at run time, or ``None`` for a plain step.

        ``FanOutStep`` with a template and ``LoopStep`` return one. ``dag()`` and ``explain_key``
        read it.
        """
        return None

    def content_hash(self) -> str | None:
        """Return the step's own hash (class, code and fields), or ``None`` when a field blocks it.

        Identity is ``kind`` when set, else the class name. The class source is hashed in both cases,
        so a code edit changes the hash. Returns ``cache_key`` unchanged when the class sets it.

        - ``Config`` fields: skipped.
        - ``FileSource`` fields: hashed by file content. A missing file blocks the hash.
        - A field that holds a ``Step``: hashed by that step's hash.
        - Other values: encoded by ``adag.hashing.encode_value``.

        Raises:
            UnhashableValueError: a field has no stable encoding and ``strict_content_hash`` is True.
        """
        pinned = type(self).cache_key
        if pinned is not None:
            return pinned

        h = hashlib.sha256()
        for _, _, _, chunk in self._key_chunks():
            if chunk is None:
                return None
            h.update(chunk)
        return h.hexdigest()[:12]

    def _key_chunks(self) -> Iterator[tuple[ComponentKind, str, Any, bytes | None]]:
        """Yield each component of this step's key: ``(kind, name, value, bytes)``.

        ``content_hash`` feeds the bytes to sha256 in this order; ``explain_key`` reads the same
        tuples. ``None`` bytes block the key. Empty bytes add nothing.
        """
        kind = type(self).kind
        if kind:
            yield ComponentKind.IDENTITY, "", kind, b"KIND:" + encode_value(kind)
        else:
            name = type(self).__name__
            yield ComponentKind.IDENTITY, "", name, b"STEP:" + encode_value(name)
        # Source is hashed for kind-tagged steps too: editing a step's code must
        # invalidate its results whether or not it has a serialization kind.
        code_bytes = class_source_hash(type(self))
        if code_bytes:
            yield ComponentKind.CODE, "", code_bytes.hex()[:12], b"CODE:" + code_bytes

        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if field.name == "node_label":
                # Display-only label for the graph renderer, not compute identity.
                yield ComponentKind.SKIPPED, field.name, value, b""
                continue
            chunk = self._hash_field(field.name, value)
            if isinstance(value, Config) or chunk == b"":
                component = ComponentKind.SKIPPED
            elif isinstance(value, FileSource):
                component = ComponentKind.FILE
            elif isinstance(value, Step):
                component = ComponentKind.NESTED_STEP
            else:
                component = ComponentKind.FIELD
            yield component, field.name, value, chunk

    def _hash_field(self, field_name: str, value: Any) -> bytes | None:
        """Hash a single dataclass field for content_hash.

        Returns:
          - `bytes` to mix into the parent hash (empty bytes = skip).
          - `None` to block the parent hash entirely (unsatisfied input).

        Raises:
            UnhashableValueError: the field type has no stable encoding. Wrap it in
                ``Config(...)``, or set ``strict_content_hash = False`` to skip it.
        """
        name = encode_value(field_name)
        if isinstance(value, Config):
            return b""
        if isinstance(value, FileSource):
            file_content_hash = value.content_hash()
            if file_content_hash is None:
                return None
            return b"FILE:" + name + encode_value(file_content_hash)
        if isinstance(value, Step):
            sub = value.content_hash()
            if sub is None:
                return None
            return b"SUB:" + name + encode_value(sub)
        try:
            return (
                b"VALUE:"
                + name
                + encode_value(value, f"field {field_name!r} of {type(self).__name__}")
            )
        except UnhashableValueError:
            if type(self).strict_content_hash:
                raise
            return b""

    def save_artifacts(self, path: str) -> None:
        """Write step.json with the step's kind and serialized config fields.

        Default implementation captures the dataclass fields that ``_serialize_step_field``
        recognises (scalars, Path, UUID, Enum, BaseModel, composites thereof).
        Fields representing runtime state (Step instances, tokenizers, HF clients,
        model handles) are skipped: those subclasses override this method, call
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
    """A step whose body awaits I/O (a DB session, an HTTP/LLM call). Implement ``arun`` (async) instead of
    ``run`` (sync), and resolve it through ``Pipeline.aexecute`` / ``StepResult.aresolve``.

    An AsyncStep has no synchronous body: it does not override ``run``, so the synchronous path
    (``execute``/``resolve``) hits ``Step.run``'s default, which raises a clear message: a sync
    resolve cannot await an async body. Sync steps are unaffected: their inherited ``arun`` just
    runs ``run``, so a graph can freely MIX sync and async steps and resolve the whole thing with
    ``aexecute`` (each sync step runs inline, each async step is awaited).

    Per-request resources (e.g. a SQLAlchemy ``AsyncSession``) flow in like any other input: wire an
    ``InputParam`` and pass it to ``aexecute``. Nothing special is needed on the context.
    """

    @abstractmethod
    async def arun(self, **kwargs: Any) -> Out:
        """Execute the async step logic. Receives resolved input values as kwargs."""
        ...


def _overrides_content_hash(cls: type) -> bool:
    """Whether a step class, or a base below ``Step``, defines its own ``content_hash``."""
    for klass in cls.__mro__:
        if klass is Step:
            return False
        if "content_hash" in vars(klass):
            return True
    return False


KEY_RECORD_PREFIX = ".keys/"
"""Store key prefix of the key records written after each save: ``.keys/<key>.json``."""


class _Miss:
    """Sentinel for a cache/memo miss, deliberately not ``None``: a step may
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
        """Runtime type check at wiring time. Best-effort: skips if hints fail. An ``AsyncStep``'s input
        types live on ``arun`` (it has no synchronous ``run``), so validate against the step's real body.
        """
        target = type(self.step).arun if isinstance(self.step, AsyncStep) else type(self.step).run
        try:
            run_hints = get_type_hints(target)
        except Exception as exc:
            # Best-effort: a forward ref that can't resolve (missing import, stringized
            # annotation) means we can't type-check the wiring. Log it rather than fail
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

    def _flight_key(self, context: ResolveContext) -> int | str:
        """The key of this node in the in-flight registries: its content key, else its id.

        Two node objects with one content key are one computation, so concurrent resolutions
        of both run the body once: the second finds the first one's result in the cache. Without
        a result store there is no cache to find it in, so the node keeps its id. So does a node
        with no key.
        """
        if context.result_store() is None:
            return id(self)
        try:
            key = self.content_hash(params=context.params)
        except UnhashableValueError:
            return id(self)
        return id(self) if key is None else key

    def _memo_get(self, context: ResolveContext) -> Out | _Miss:
        """This node's memoized result in ``context``, or ``_CACHE_MISS``. Hold ``_lock``.

        Entries are ``(node, value)`` keyed by ``id(node)``. Holding the node keeps its id from
        being reused while the entry exists, and the identity check rejects a stale entry.
        """
        entry = context._resolved.get(id(self))
        if entry is not None and entry[0] is self:
            return entry[1]
        return _CACHE_MISS

    def _check_cache(self, context: ResolveContext) -> Out | _Miss:
        """The shared, synchronous *front* of ``resolve``/``aresolve``: per-execute memo
        lookup, ``on_step_start``, and the disk-cache hit path. Returns the resolved value
        on a memo/cache hit, or ``_CACHE_MISS`` if the step still has to run.

        Disk IO is synchronous on both paths. It is not the bottleneck and keeps the cache
        format byte-identical. ``context._lock`` guards the memo dict so the parallel
        executor's concurrent nodes are safe regardless of the GIL.
        """
        # Per-execute memoization, by id(). The memo holds the node too (see _memo_get), so a
        # context that outlives its graph cannot hand one node's result to another.
        with context._lock:
            memo = self._memo_get(context)
        if not isinstance(memo, _Miss):
            return memo

        step_name = type(self.step).__name__
        # Announce entry BEFORE the recursive hash/cache check, so progress shows the instant
        # a step is reached. Firing on_step_start before source resolution also brackets every
        # child's start/finish inside this step's. That nesting is what the RunTreeObserver
        # turns back into the execution tree.
        context.observer.on_step_start(step_name)

        if self.step.caches_to_path():
            loaded = self._try_load_path(self._result_dir(context), context.params)
        else:
            store = context.result_store()
            loaded = self._try_load(store, params=context.params) if store is not None else None
        if loaded is not None:
            if context.on_progress:
                context.on_progress(Progress(step=step_name, message="cache hit"))
            with context._lock:
                context._resolved[id(self)] = (self, loaded)
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
        if self.step.caches_to_path():
            cache_dir = self._result_dir(context)
            if cache_dir is not None:
                result = self._save_path(result, cache_dir, context.params)
                emit("step.saved", step=step_name, key=self.content_hash(params=context.params))
                self._write_key_record(context)
        else:
            store = context.result_store()
            if store is not None and self.step.result_model() is not None:
                self._save(result, store, params=context.params)
                emit("step.saved", step=step_name, key=self.content_hash(params=context.params))
                self._write_key_record(context)
        with context._lock:
            context._resolved[id(self)] = (self, result)
        return result

    def _write_key_record(self, context: ResolveContext) -> None:
        """Write this node's key explanation to ``.keys/<key>.json`` in the result store.

        The record lets a later release compare a new key with the one a result was saved
        under. A failed write never fails the run: it emits ``step.key_record.failed``.
        """
        store = context.result_store()
        key = self.content_hash(params=context.params)
        if store is None or key is None:
            return
        try:
            from adag import __version__  # late: adag/__init__ imports this module

            record = {
                "record_version": 1,
                "adag_version": __version__,
                "saved_at": datetime.now(UTC).isoformat(),
                **self.explain_key(context.params).redacted().to_dict(),
            }
            store.set(f"{KEY_RECORD_PREFIX}{key}.json", json.dumps(record, indent=1))
        except Exception as exc:
            emit("step.key_record.failed", step=type(self.step).__name__, key=key, error=str(exc))

    def resolve(self, context: ResolveContext) -> Out:
        """Resolve upstream sources, run the step, cache if applicable.

        Memoizes by ``id(self)`` so multiple downstream consumers of the SAME ``StepResult``
        share one computed result within a ``Pipeline.execute``. This is critical for non-cached
        steps returning non-serializable objects (e.g. model handles), where consumers must
        see the same instance, not two re-executions. The cache/memo/observer skeleton lives
        in ``_check_cache``/``_finalize`` (shared with ``aresolve``); only the two lines that
        genuinely differ (upstream resolution and the step body) stay here, run inline.

        In-flight dedup makes that guarantee hold under CONCURRENCY too: the memo only
        collapses resolutions that do not overlap in time, so N branch workers reaching one
        shared upstream at once would each miss the memo and run the body N times. Here the
        first thread to miss the memo claims ownership (an ``Event`` in ``context._inflight``, keyed by
        the node's content key, so two node objects with one key count as one);
        concurrent threads find that Event, wait on it, then loop: a success left the value in
        the memo (returned), a failure removed the marker so they re-claim and resolve it
        themselves. Uncontended (single-threaded) there is never a waiter, so behaviour and
        cost are unchanged bar creating one Event that is set immediately.
        """
        flight_key = self._flight_key(context)
        while True:
            with context._lock:
                memo = self._memo_get(context)
                if not isinstance(memo, _Miss):
                    return memo
                in_flight = context._inflight.get(flight_key)
                if in_flight is None:
                    own_event = Event()
                    context._inflight[flight_key] = own_event
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
                context._inflight.pop(flight_key, None)
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
        """Resolve this node on the event loop and return its result. Public API.

        Use it to schedule async steps yourself, for example one ``asyncio.gather`` per
        topological level inside an async request handler. Pass the same ``ResolveContext`` to
        every call: it memoizes each node, so a shared upstream runs once and a later call reuses
        earlier results. Upstream nodes are awaited, async step bodies run on the loop, and sync
        bodies run unchanged. A cancelled task starts no further step.

        Gathered calls that reach the same node at the same time run its body once: the first
        task claims the node and the others wait for it. If that body raises, a waiting task
        resolves the node itself.
        """
        flight_key = self._flight_key(context)
        while True:
            with context._lock:
                memo = self._memo_get(context)
                if not isinstance(memo, _Miss):
                    return memo
                in_flight = context._ainflight.get(flight_key)
                if in_flight is None:
                    own = asyncio.get_running_loop().create_future()
                    context._ainflight[flight_key] = own
                    break
            # Another task owns this node. Wait without taking its exception, then loop: a
            # success left the value in the memo, a failure removed the marker.
            await asyncio.wait([in_flight])
        try:
            return await self._aresolve_owned(context)
        finally:
            with context._lock:
                context._ainflight.pop(flight_key, None)
            if not own.done():
                own.set_result(None)

    async def _aresolve_owned(self, context: ResolveContext) -> Out:
        """Resolve this node on the loop, once this task owns it (see ``aresolve``)."""
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
        h = hashlib.sha256()
        for _, _, _, chunk in self._key_chunks(params):
            if chunk is None:
                return None
            h.update(chunk)
        return h.hexdigest()[:12]

    def explain_key(self, params: dict[str, Any] | None = None) -> KeyExplanation:
        """List every component of this node's cache key.

        Use it when a node recomputes and you do not know why. The components are the step's
        identity, code digest and fields, then one per input. The key is built from the same
        components, so ``explain_key(p).key == content_hash(p)``.

        Args:
            params: the run's params, as passed to ``execute``.
        """
        step = self.step
        components: list[KeyComponent] = []
        blocked_by: str | None = None
        pinned = type(step).cache_key
        if pinned is not None:
            components.append(KeyComponent.build(ComponentKind.PINNED, "", pinned, pinned.encode()))
        elif _overrides_content_hash(type(step)):
            # A subclass with its own content_hash: its parts are not visible to adag.
            own = step.content_hash()
            chunk = None if own is None else own.encode()
            components.append(KeyComponent.build(ComponentKind.IDENTITY, "", own, chunk))
            if own is None:
                blocked_by = "content_hash"
        else:
            for kind, name, value, chunk in step._key_chunks():
                components.append(KeyComponent.build(kind, name, value, chunk))
                if chunk is None and blocked_by is None:
                    blocked_by = name or kind.value
        expansion = step.expansion()
        if expansion is not None:
            components.append(
                KeyComponent.build(ComponentKind.EXPANSION, expansion.shape, expansion.note, b"")
            )
        for kind, name, value, chunk in self._key_chunks(params):
            if kind is ComponentKind.IDENTITY:
                continue  # the step's own key: already listed component by component above
            shown: Any = value
            if isinstance(value, StepResult):
                upstream: StepResult[Any] = value  # pyright: ignore[reportUnknownVariableType]
                shown = f"{type(upstream.step).__name__} {upstream.content_hash(params=params)}"
            components.append(KeyComponent.build(kind, name, shown, chunk))
            if chunk is None and blocked_by is None:
                blocked_by = name or kind.value
        label = getattr(step, "node_label", None) or None
        return KeyExplanation(
            step=type(step).__name__,
            label=label,
            key=self.content_hash(params=params),
            blocked_by=blocked_by,
            key_format=KEY_FORMAT,
            components=tuple(components),
        )

    def _key_chunks(
        self, params: dict[str, Any] | None
    ) -> Iterator[tuple[ComponentKind, str, Any, bytes | None]]:
        """Yield each component of this node's key: ``(kind, name, value, bytes)``.

        The first is the step's own key; then one per input, sorted by name. ``None`` bytes
        block the key (a missing file, an upstream with no key).
        """
        step_hash = self.step.content_hash()
        yield (
            ComponentKind.IDENTITY,
            "",
            step_hash,
            None if step_hash is None else step_hash.encode(),
        )

        for name, source in sorted(self.sources.items()):
            prefix = encode_value(name)
            if isinstance(source, StepResult):
                upstream_hash = source.content_hash(params=params)
                chunk = (
                    None if upstream_hash is None else prefix + b"UP:" + encode_value(upstream_hash)
                )
                yield ComponentKind.UPSTREAM, name, source, chunk
            elif isinstance(source, InputParam):
                if params and source.name in params:
                    param_value: Any = params[source.name]
                elif source.has_default:
                    param_value = source.default
                else:
                    param_value = None
                where = f"value of InputParam {source.name!r}"
                chunk = (
                    prefix
                    + b"PARAM:"
                    + encode_value(source.name)
                    + encode_value(param_value, where)
                )
                yield ComponentKind.PARAM, name, param_value, chunk
            elif isinstance(source, Deferred):
                # A Deferred's own content_hash (FileSource hashes file bytes) must reach the
                # key, or editing a tracked file would not invalidate anything downstream.
                deferred_hash = source.content_hash() if hasattr(source, "content_hash") else None
                chunk = (
                    prefix
                    + b"DEFERRED:"
                    + encode_value(type(source).__name__)
                    + encode_value(deferred_hash)
                )
                yield ComponentKind.DEFERRED, name, source, chunk
            else:
                where = f"input {name!r} of {type(self.step).__name__}"
                chunk = prefix + b"RAW:" + encode_value(source, where)
                yield ComponentKind.RAW, name, source, chunk

    def is_cached(self, where: Path | CacheStore, params: dict[str, Any] | None = None) -> bool:
        """Return True when a complete, valid result for this node is in ``where``.

        ``where`` is a cache directory or a ``CacheStore``. For a step that overrides
        ``save_result``, ``where`` must be a directory, and the check calls ``load_result``: an
        entry that ``load_result`` rejects is not cached, the same answer the resolver gives.
        This makes the check as costly as one load for such a step.
        """
        if self.step.caches_to_path():
            return self._try_load_path(_path_root(where, self.step), params) is not None
        return self._try_load(where, params=params) is not None

    def _result_dir(self, context: ResolveContext) -> Path | None:
        """The directory for a path result: ``cache_dir``. A ``store`` cannot hold a path.

        Raises:
            ValueError: the run has a ``store`` and no ``cache_dir``. Raised before the body runs.
        """
        if context.cache_dir is None and context.store is not None:
            raise ValueError(
                f"{type(self.step).__name__} caches to a path (it overrides save_result); "
                "a CacheStore holds only text. Pass cache_dir= as well."
            )
        return context.cache_dir

    def _try_load_path(self, cache_dir: Path | None, params: dict[str, Any] | None) -> Out | None:
        """Load a path result with ``load_result``; None on a miss or an invalid entry."""
        key = self.content_hash(params=params)
        if cache_dir is None or key is None:
            return None
        path = cache_dir.joinpath(key)
        if not cache_dir.joinpath(key + ".done").exists() or not path.exists():
            return None
        try:
            return self.step.load_result(path)
        except Exception as exc:
            emit("step.cache.invalid_entry", step=type(self.step).__name__, key=key, error=str(exc))
            return None

    def _save_path(self, result: Out, cache_dir: Path, params: dict[str, Any] | None) -> Out:
        """Save with ``save_result`` to a temp path, rename it into place, write the marker, and
        return ``load_result`` of the final path.

        Returning the reloaded value means a fresh run and a cached run hand downstream the same
        thing, and a result that refers to its own location gets the final path.
        """
        key = self.content_hash(params=params)
        if key is None:
            return result
        cache_dir.mkdir(parents=True, exist_ok=True)
        final = cache_dir.joinpath(key)
        tmp = cache_dir.joinpath(f"{key}.tmp-{os.getpid()}-{uuid4().hex[:8]}")
        try:
            self.step.save_result(result, tmp)
            cache_dir.joinpath(key + ".done").unlink(missing_ok=True)
            _remove_path(final)
            tmp.rename(final)
        finally:
            _remove_path(tmp)
        atomic_write_text(cache_dir.joinpath(key + ".done"), datetime.now(UTC).isoformat())
        reloaded = self.step.load_result(final)
        step_cls = type(self.step)
        if step_cls not in _ROUND_TRIP_WARNED and _round_trip_differs(result, reloaded):
            _ROUND_TRIP_WARNED.add(step_cls)
            logger.warning(
                "step.load_result_differs",
                step=step_cls.__name__,
                hint="downstream gets load_result's value, also on the first run; "
                "load_result must rebuild everything run put in the result",
            )
        return reloaded

    def _try_load(
        self, where: Path | CacheStore, params: dict[str, Any] | None = None
    ) -> Out | None:
        """Read and validate the cached result in one pass; None on any miss.

        A missing ``.done`` marker, an empty value, or a value the result model rejects is a
        miss, never a crash. Invalid entries are reported so a broken cache surfaces.
        """
        result_type = self.step.result_model()
        key = self.content_hash(params=params)
        if result_type is None or key is None:
            return None
        store = _as_store(where)
        if not store.exists(key + ".done"):
            return None
        text = store.get(key)
        if text is None:
            return None
        try:
            return result_type.model_validate_json(text)  # pyright: ignore[reportReturnType]
        except Exception as exc:
            emit("step.cache.invalid_entry", step=type(self.step).__name__, key=key, error=str(exc))
            return None

    def _save(
        self, result: Out, where: Path | CacheStore, params: dict[str, Any] | None = None
    ) -> None:
        key = self.content_hash(params=params)
        if key is None:
            return
        if not isinstance(result, BaseModel):
            raise TypeError(
                f"Cannot cache {type(result).__name__}: result_type is set but result is not a BaseModel"
            )
        store = _as_store(where)
        new_text = result.model_dump_json(indent=2)
        # Invariant: same content_hash -> same bytes. Overwriting an existing hash with
        # different bytes means the cache key is missing an input; log loudly (the message
        # below has the likely causes) rather than silently ship stale results.
        old_text = store.get(key)
        if old_text is not None and old_text != new_text:
            logger.error(
                "cached_step.invariant_violation",
                step=type(self.step).__name__,
                hash=key,
                message=(
                    "Same content_hash produced different bytes. The "
                    "step's cache key is missing an input — likely a "
                    "side-channel file (model weights, embeddings) or "
                    "a sub-batch whose item keys do not encode all "
                    "inputs. Overwriting cache with new bytes."
                ),
            )
        store.set(key, new_text)
        store.set(key + ".done", datetime.now(UTC).isoformat())

    def load(self, where: Path | CacheStore, params: dict[str, Any] | None = None) -> Out:
        """Load the cached result from a cache directory or a ``CacheStore``.

        Raises:
            ValueError: no result is stored for this node.
        """
        if self.step.caches_to_path():
            loaded = self._try_load_path(_path_root(where, self.step), params)
            if loaded is None:
                raise ValueError(f"Output for '{type(self.step).__name__}' not found.")
            return loaded
        key = self.content_hash(params=params)
        result_type = self.step.result_model()
        text = _as_store(where).get(key) if key is not None else None
        if text is None or result_type is None:
            raise ValueError(f"Output for '{type(self.step).__name__}' not found.")
        return result_type.model_validate_json(text)  # pyright: ignore[reportReturnType]

    def __repr__(self) -> str:
        return f"StepResult({type(self.step).__name__})"


def _path_root(where: Path | CacheStore, step: Step[Any]) -> Path:
    """The directory of a path result: ``where`` itself, or a ``FileSystemCacheStore``'s root.

    Raises:
        TypeError: ``where`` is a store that cannot hold a path.
    """
    if isinstance(where, Path):
        return where
    if isinstance(where, FileSystemCacheStore):
        return where.root
    raise TypeError(
        f"{type(step).__name__} caches to a path (it overrides save_result); pass a cache "
        f"directory, not a {type(where).__name__}."
    )


def _remove_path(path: Path) -> None:
    """Delete a file or a directory tree at ``path``, if any."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _as_store(where: Path | CacheStore) -> CacheStore:
    """A cache directory becomes a ``FileSystemCacheStore``; a store is returned as is."""
    if isinstance(where, Path):
        return FileSystemCacheStore(where)
    return where


# Defined after StepResult so the self-reference resolves. With
# from __future__ import annotations, all annotations in this module
# are lazy strings: _SourceValue is looked up at type-check time.
_SourceValue: TypeAlias = (
    StepResult[Any] | InputParam[Any] | FileSource | Config[Any] | str | int | float | bool | None
)
