"""Pipeline: wraps a Deferred graph and provides execute().

A Pipeline is constructed from a StepResult (the final output of a DAG).
Executing the pipeline resolves the full graph with parameters and caching.

    tokenizer = Tokenizer(max_length=256)
    text = InputParam("text")
    tokens = tokenizer(text=text)

    pipeline = Pipeline(tokens, cache_dir=Path("cache"))
    result = pipeline.execute(text="hello world")

When cache_dir is None (the default for from_directory), step results are
neither read from nor written to disk. Every execute() runs the full DAG.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import math
import os
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from functools import partial
from pathlib import Path
from typing import Any, Generic

import structlog
from pydantic import BaseModel
from typing_extensions import TypeVar  # default= (PEP 696) needs this on Python 3.12

from adag import _store_io
from adag.cache_store import CacheStore
from adag.deferred import (
    AsyncExecutor,
    BranchDispatcher,
    Deferred,
    Executor,
    InlineDispatcher,
    InputParam,
    LocalAsyncExecutor,
    LocalExecutor,
    PipelineObserver,
    Progress,
    ResolveContext,
    gather_first_failure,
    observing,
)
from adag.deprecation import warn_deprecated
from adag.hashing import UnhashableValueError
from adag.hooks import HOOK_NAMES, takes_any_keyword
from adag.io import atomic_writer
from adag.item_cache import ItemCache
from adag.key import Expansion, KeyExplanation
from adag.observers import LoggingObserver
from adag.step import Step, StepResult, _serialize_step_field

logger = structlog.get_logger()

R = TypeVar("R", bound=BaseModel)
# `Out` is the type produced by the canonical output step; `pipeline.execute()`
# returns this. Defaulting to `Any` (PEP 696) keeps unannotated callers
# unchanged (`Pipeline` without a type arg gives `Any`) while
# `Pipeline(my_typed_step_result)` flows the typed result into execute()'s
# return.
Out = TypeVar("Out", default=Any)


type JsonValue = bool | int | float | str | Sequence[JsonValue] | Mapping[str, JsonValue] | None
"""A value ``json.dumps`` accepts: the type of ``Pipeline.metadata``.

``Sequence`` and ``Mapping`` (not ``list`` and ``dict``) so a ``dict[str, int]`` is accepted under
strict type checking. Narrow it (``isinstance``) before you use a part of it.
"""


_MAX_TEMPLATE_DEPTH = 4
"""``validate()`` looks this many templates deep. A template that builds itself stops there."""


class CyclicPipelineError(ValueError):
    """Raised at construction when the Deferred graph contains a cycle (a step transitively depends on itself)."""


# Hooks that adag calls with the item or the results as their first positional argument.
_POSITIONAL_FIRST = frozenset({"process_item", "assemble"})


class UnboundInputError(ValueError):
    """Raised by Pipeline.validate() when a step has a required run() input that is neither wired nor a dataclass field."""


class UnacceptedInputError(TypeError):
    """Raised by Pipeline.validate() when a step wires an input that a hook does not accept.

    The hook (``run``, ``arun``, ``items_data``, ``process_item`` or ``assemble``) does not name
    the input and has no ``**kwargs``, so ``execute()`` would fail with the same message as a
    ``TypeError`` for an unexpected keyword argument.
    """


@dataclasses.dataclass(frozen=True)
class StepExpansion:
    """A step that builds a sub-graph at run time, and the steps of that sub-graph.

    ``body`` is the template traced over placeholders, each step after the steps it uses. It is
    ``None`` when the template cannot be built over placeholders.
    """

    step: Step[Any]
    expansion: Expansion
    body: tuple[StepResult[Any], ...] | None


class Pipeline(Generic[Out]):
    """Wraps a Deferred graph. Discovers steps and parameters, provides execute().

    tokenizer = Tokenizer(max_length=256)
    text = InputParam("text")
    tokens = tokenizer(text=text)
    pipeline = Pipeline(tokens, cache_dir=Path("cache"))
    result = pipeline.execute(text="hello world")

    With neither ``cache_dir`` nor ``store`` (the default), nothing is cached and every
    ``execute()`` runs the full graph. Pass ``store`` to cache in a ``CacheStore`` instead of
    files under ``cache_dir``.
    """

    def __init__(
        self,
        output: StepResult[Out],
        cache_dir: Path | None = None,
        on_progress: Callable[[Progress], None] | None = None,
        *,
        kind: str | None = None,
        extra_targets: list[StepResult[Any]] | None = None,
        store: CacheStore | None = None,
        metadata: JsonValue = None,
    ) -> None:
        """Wrap a Deferred graph.

        Args:
            output: the canonical terminal node of the DAG. ``execute()``
                without an argument resolves this.
            cache_dir: directory for step results. With neither ``cache_dir`` nor ``store``,
                nothing is cached.
            store: a ``CacheStore`` for step results and ``BatchStep`` item caches, used
                instead of files under ``cache_dir``.
            on_progress: optional progress callback (passed to ``ResolveContext``).
            kind: a tag for the pipeline, for example ``"report/v1"``. ``save()`` writes it to
                ``pipeline.json``.
            metadata: caller data saved with the pipeline: any JSON value (a string, a number, a
                list, a dict). ``save()`` writes it to ``pipeline.json`` and ``from_directory()``
                reads it back into ``pipeline.metadata``. adag never reads it otherwise.
            extra_targets: additional terminal or intermediate nodes whose
                sub-DAGs should be discovered too. Use this when the
                pipeline has multiple "ending points" so visualization and
                ``resolve()`` can reach every step in the graph (not just
                the ones reachable from ``output``). Use
                ``Pipeline.from_targets([...])`` for a slightly nicer call
                site when you have no canonical terminal.
        """
        self.output = output
        self.cache_dir = cache_dir
        self.store = store
        self.on_progress = on_progress
        self.kind = kind
        if metadata is not None:
            try:
                json.dumps(metadata)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"Pipeline metadata must be a JSON value: {exc}") from exc
        self.metadata: JsonValue = metadata

        self._steps: list[Step[Any]] = []
        self._inputs: list[InputParam[Any]] = []
        self._targets: list[StepResult[Any]] = [output]
        self._discover(output)
        for extra in extra_targets or ():
            if extra is not output and extra not in self._targets:
                self._targets.append(extra)
                self._discover(extra)

    @classmethod
    def from_targets(
        cls,
        targets: list[StepResult[Any]],
        cache_dir: Path | None = None,
        on_progress: Callable[[Progress], None] | None = None,
        *,
        kind: str | None = None,
        store: CacheStore | None = None,
        metadata: JsonValue = None,
    ) -> Pipeline[Any]:
        """Build a Pipeline that covers multiple terminal nodes.

        Real DAGs often have more than one ending point (e.g. the main
        result plus a separate audit / dedup artifact). Pass them all
        here and the Pipeline discovers the union of their sub-graphs.
        The first target is treated as the canonical ``output`` so a
        zero-arg ``execute()`` still has a single thing to return.
        Downstream callers use ``resolve(target)`` to pick any of them.
        """
        if not targets:
            raise ValueError("Pipeline.from_targets requires at least one target.")
        head, *rest = targets
        return cls(
            output=head,
            cache_dir=cache_dir,
            on_progress=on_progress,
            kind=kind,
            extra_targets=rest,
            store=store,
            metadata=metadata,
        )

    @property
    def targets(self) -> list[StepResult[Any]]:
        """All terminal / target nodes the pipeline was built to resolve."""
        return list(self._targets)

    def _discover(self, deferred: Deferred[Any], visiting: set[int] | None = None) -> None:
        """Walk the Deferred graph backward to collect all steps and parameters.

        ``visiting`` is the current DFS path (StepResult ids). Re-entering a node already
        on the path means a cycle, we raise CyclicPipelineError instead of letting the
        recursion blow up as an opaque RecursionError deep in resolve(). Steps are appended
        after their sources, so ``self._steps`` ends up in dependency (topological) order.
        """
        visiting = visiting if visiting is not None else set()
        if isinstance(deferred, InputParam) and deferred not in self._inputs:
            self._inputs.append(deferred)
        elif isinstance(deferred, StepResult):
            if id(deferred) in visiting:
                label = type(deferred.step).label or type(deferred.step).__name__
                raise CyclicPipelineError(f"Pipeline graph contains a cycle through step {label!r}")
            visiting.add(id(deferred))
            for source in deferred.sources.values():
                if isinstance(source, Deferred):
                    self._discover(source, visiting)
            visiting.discard(id(deferred))
            if deferred.step not in self._steps:
                self._steps.append(deferred.step)

    @property
    def steps(self) -> list[Step[Any]]:
        """All steps in dependency (topological) order.

        Every step appears after the steps it depends on. The post-order discovery walk in
        ``_discover`` guarantees it.
        """
        return list(self._steps)

    def topological_order(self) -> list[Step[Any]]:
        """Steps in a guaranteed topological order (each step after its dependencies).

        A named contract (delegates to ``steps``). Consumers that depend on the ordering
        (deterministic execution, left-to-right layered layout, level grouping) rely on this
        rather than on the ``steps`` discovery detail.
        """
        return self.steps

    def topological_levels(self) -> list[list[Step[Any]]]:
        """Return steps grouped into dependency levels.

        Every step lands one level past its deepest dependency. Steps within a level are
        mutually independent, so a concurrent executor can run a whole level in parallel;
        level i+1 starts once level i is resolved.

        Pure analysis: no execution change.
        """
        # step -> upstream steps, walked from the targets by StepResult id (no cached map).
        upstreams: defaultdict[Step[Any], set[Step[Any]]] = defaultdict(set)
        seen: set[int] = set()
        stack: list[StepResult[Any]] = list(self._targets)
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            for source in node.sources.values():
                if isinstance(source, StepResult):
                    upstreams[node.step].add(source.step)
                    stack.append(source)

        # self._steps is already topological, so each step's upstreams have a level by now.
        level_of: dict[Step[Any], int] = {}
        levels: list[list[Step[Any]]] = []
        for step in self._steps:
            dependencies = upstreams.get(step, set())
            level = (
                0
                if not dependencies
                else 1 + max(level_of[dependency] for dependency in dependencies)
            )
            level_of[step] = level
            if level == len(levels):
                levels.append([])
            levels[level].append(step)
        return levels

    def _stepresult_levels(self) -> list[list[StepResult[Any]]]:
        """The graph's StepResult nodes grouped into dependency levels (Kahn over node ids)."""
        return _node_levels(self._targets)

    def _resolve_levels_concurrently(
        self, context: ResolveContext, max_workers: int | None
    ) -> None:
        """Resolve the whole graph level by level, each level's nodes in a thread pool.

        Steps within a level are independent, so they run concurrently; the next level
        starts only once this one is fully resolved. ``resolve`` memoises by ``id`` and
        every node's upstreams are resolved in earlier levels, so each node finds its inputs
        already in ``context._resolved`` and just runs its own step, no recursion, no step
        run twice.

        Each StepResult is resolved exactly once (in its level), with upstream keys written
        in earlier levels and only read here. ``context._lock`` (see ResolveContext) guards
        every ``_resolved`` read/write, so concurrent nodes are safe regardless of the GIL. It
        does not rely on CPython dict-op atomicity, which the free-threaded build removes.

        Each node runs inside a copied context (``copy_context().run``), so ambient ContextVars
        (the usage observer, ``current_observer``) propagate into the level workers, which a raw
        ThreadPoolExecutor submit would not inherit.
        """
        for level in self._stepresult_levels():
            if len(level) == 1:
                level[0].resolve(context)
                continue
            with ThreadPoolExecutor(max_workers=max_workers or len(level)) as pool:
                futures = [pool.submit(copy_context().run, node.resolve, context) for node in level]
                for future in as_completed(futures):
                    future.result()

    def explain_keys(self, params: dict[str, Any] | None = None) -> tuple[KeyExplanation, ...]:
        """Explain the cache key of every node, upstream nodes first.

        Args:
            params: the run's params, as passed to ``execute``.
        """
        return tuple(
            node.explain_key(params) for level in self._stepresult_levels() for node in level
        )

    def validate(self) -> None:
        """Validate the graph before ``execute()``: every step's required run() input is bound.

        Cycles are already rejected at construction (see ``_discover``). This adds the
        missing-input check: a required parameter of a step's ``run()`` that is neither a
        wired source nor one of the step's dataclass fields would otherwise fail late at
        ``execute()`` as a bare ``TypeError: run() missing 1 required argument``. Opt-in
        (not run at construction) so a consumer building a graph from dynamic/untrusted
        input can validate it up front and reject with a 4xx instead of crashing
        mid-resolution. Raises ``UnboundInputError`` naming the step and the unbound params.

        It also checks the steps inside each template (a fan-out template, a loop body), built
        over placeholders, so a step with a missing input is found before the run reaches it.
        A template that cannot be built over placeholders is skipped.

        Walks the target StepResults directly (visited by id, the same node identity
        ``resolve()`` uses) rather than caching a derived step->sources map.
        """
        seen: set[int] = set()
        stack: list[StepResult[Any]] = list(self._targets)
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            self._check_inputs_bound(node)
            stack.extend(
                source for source in node.sources.values() if isinstance(source, StepResult)
            )
        self._validate_templates(self.expansions())

    def expansions(self) -> tuple[StepExpansion, ...]:
        """Return the steps that build a sub-graph at run time (a template fan-out, a loop).

        One entry for each such step of the pipeline, in the order of ``steps``. Steps inside a
        template are not listed: read ``StepExpansion.body`` and ask each step for its
        ``expansion()``.
        """
        found: list[StepExpansion] = []
        for step in self._steps:
            expansion = step.expansion()
            if expansion is not None:
                found.append(StepExpansion(step, expansion, expansion.template.steps()))
        return tuple(found)

    def _validate_templates(self, found: Sequence[StepExpansion], depth: int = 0) -> None:
        """Check the steps of every template, and of the templates inside them."""
        if depth >= _MAX_TEMPLATE_DEPTH:
            return
        for entry in found:
            label = type(entry.step).label or type(entry.step).__name__
            for node in entry.body or ():
                try:
                    self._check_inputs_bound(node)
                except (UnboundInputError, UnacceptedInputError) as error:
                    raise type(error)(f"In the template of {label!r}: {error}") from error
            inner = [
                StepExpansion(n.step, e, e.template.steps())
                for n in entry.body or ()
                if (e := n.step.expansion()) is not None
            ]
            self._validate_templates(inner, depth + 1)

    def _check_inputs_bound(self, node: StepResult[Any]) -> None:
        """Check the step's hooks against what ``execute()`` passes them.

        Raises ``UnboundInputError`` if a hook has a required parameter that is neither a wired
        source nor one of the step's dataclass fields: both feed the hook (see
        ``StepResult._merge_step_defaults``), so either satisfies the parameter. Raises
        ``UnacceptedInputError`` if a wired source is not accepted by a hook that has no
        ``**kwargs``. A dataclass field that a hook does not name is allowed: the hook reads it
        from ``self`` (#15).
        """
        step_cls = type(node.step)
        field_names = {field.name for field in dataclasses.fields(node.step)}
        available = set(node.sources.keys()) | field_names
        label = step_cls.label or step_cls.__name__
        # A custom execute() decides itself which inputs reach which method.
        standard_execute = getattr(step_cls.execute, "__module__", "") in (
            "adag.step",
            "adag.batch",
        )
        for hook_name in HOOK_NAMES:
            hook = getattr(step_cls, hook_name, None)
            if hook is None:
                continue
            try:
                parameters = list(inspect.signature(hook).parameters.values())
            except (ValueError, TypeError):
                continue
            # The first parameter is self; process_item and assemble also get the item or the
            # results positionally from adag.
            supplied = 2 if hook_name in _POSITIONAL_FIRST else 1
            named = {
                p.name
                for p in parameters[supplied:]
                if p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
            }
            required = {
                p.name
                for p in parameters[supplied:]
                if p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
                and p.default is inspect.Parameter.empty
            }
            missing = required - available
            if missing:
                raise UnboundInputError(
                    f"Step {label!r} has unbound required input(s): {sorted(missing)}"
                )
            if standard_execute and not takes_any_keyword(hook):
                unaccepted = set(node.sources) - named
                if unaccepted:
                    raise UnacceptedInputError(
                        f"Step {label!r}: {hook_name}() does not accept the wired input(s) "
                        f"{sorted(unaccepted)}. Add the parameter or **kwargs."
                    )

    @property
    def input_names(self) -> set[str]:
        """Names of every InputParam this pipeline expects from execute(**kwargs).

        Public alternative to reaching into ``_inputs`` from outside the package. Callers that
        branch on "does this pipeline accept a `threshold` kwarg" should use this, so the
        private list shape stays free to evolve.
        """
        return {p.name for p in self._inputs}

    @property
    def inputs(self) -> list[InputParam[Any]]:
        """Every InputParam this pipeline expects, in discovery order.

        Public accessor for callers (e.g. the CLI) that need each param's name,
        type, and default (not just the names from ``input_names``) to build
        help text or coerce string arguments.
        """
        return list(self._inputs)

    def step_name(self, step: Step[Any]) -> str:
        """Directory name for a step: {index}_{name}, falling back to the lowercased class name."""
        index = self._steps.index(step)
        width = max(2, math.floor(math.log10(max(len(self._steps), 1))) + 1)
        name = getattr(step, "name", None) or type(step).__name__.lower()
        return f"{index:0{width}d}_{name}"

    def item_cache(self, namespace: str, schema: type[R]) -> ItemCache[R]:
        """Create an ItemCache in this pipeline's ``store``, or under its cache directory."""
        if self.store is not None:
            return ItemCache(self.cache_dir or Path(), namespace, schema, store=self.store)
        if self.cache_dir is None:
            raise ValueError("Cannot create ItemCache without a cache directory or store.")
        return ItemCache(self.cache_dir, namespace, schema)

    def execute(
        self,
        *,
        observer: PipelineObserver | None = None,
        executor: Executor | None = None,
        dispatcher: BranchDispatcher | None = None,
        parallel: bool = False,
        max_workers: int | None = None,
        **params: Any,
    ) -> Out:
        """Run the graph and return the result of ``output``.

        Args:
            observer: receives the run's hooks. Default: ``LoggingObserver``. Pass ``NullObserver``
                for silence.
            executor: where each step runs. Default: ``LocalExecutor`` (this process).
            dispatcher: where fan-out branches run. Default: ``InlineDispatcher`` (this process).
                Pass a ``SubprocessDispatcher`` or a ``RemoteDispatcher`` to run them elsewhere.
            parallel: run the independent steps of each level on a thread pool. Use it for steps
                that block on I/O. ``RunTreeObserver`` then shows a flat tree. Deprecated, removed
                in 1.0: set ``runs_on = "thread"`` on the steps and use ``aexecute``.
            max_workers: thread pool size for ``parallel``. Default: one thread per step of the
                level. Deprecated with ``parallel``: use ``aexecute(max_concurrency=)``.
            **params: one value per ``InputParam``.

        The five keyword names above are reserved: do not use them as ``InputParam`` names.

        Raises:
            ValueError: a required ``InputParam`` has no value.
        """
        if parallel or max_workers is not None:
            warn_deprecated(
                "Pipeline.execute(parallel=, max_workers=)",
                'steps with runs_on = "thread" and Pipeline.aexecute(max_concurrency=)',
            )
        missing = [p.name for p in self._inputs if p.name not in params and not p.has_default]
        if missing:
            raise ValueError(f"Missing parameters: {missing}")

        context = ResolveContext(
            cache_dir=self.cache_dir,
            store=self.store,
            params=params,
            on_progress=self.on_progress,
            observer=observer or LoggingObserver(),
            executor=executor or LocalExecutor(),
            dispatcher=dispatcher or InlineDispatcher(),
        )
        try:
            with observing(context.observer):
                if parallel:
                    self._resolve_levels_concurrently(context, max_workers)
                return self.output.resolve(context)
        finally:
            context.close()

    async def aexecute(
        self,
        *,
        observer: PipelineObserver | None = None,
        async_executor: AsyncExecutor | None = None,
        max_concurrency: int | None = 1,
        **params: Any,
    ) -> Out:
        """Run the graph on the running event loop and return the result of ``output``.

        ``AsyncStep`` bodies are awaited on the loop. A sync step body runs on the loop and blocks
        it, unless the step sets ``runs_on = "thread"`` or ``"process"``. At most ``max_concurrency``
        step bodies run at once (``aresolve_all``), and the branches of a ``FanOutStep`` count
        too. The default is 1, so a shared resource such as one database session is never used by
        two steps at once. Pass ``None`` to run a whole level together. Cancelling the task starts
        no further step.

        ``observer``, ``async_executor`` and ``max_concurrency`` are reserved: do not use them as
        ``InputParam`` names. There is no ``dispatcher``: fan-out branches run in this process.
        """
        missing = [p.name for p in self._inputs if p.name not in params and not p.has_default]
        if missing:
            raise ValueError(f"Missing parameters: {missing}")

        context = ResolveContext(
            cache_dir=self.cache_dir,
            store=self.store,
            params=params,
            on_progress=self.on_progress,
            observer=observer or LoggingObserver(),
            async_executor=async_executor or LocalAsyncExecutor(),
        )
        try:
            return (await aresolve_all([self.output], context, max_concurrency=max_concurrency))[0]
        finally:
            context.close()

    def resolve(
        self,
        target: StepResult[R],
        *,
        observer: PipelineObserver | None = None,
        executor: Executor | None = None,
        dispatcher: BranchDispatcher | None = None,
        **params: Any,
    ) -> R:
        """Resolve one node of this pipeline and return its result, from the cache when it is there.

        Use it to read an intermediate result after ``execute()``. Do not call ``step.run()`` for
        that: it skips the cache and can return a value that differs from the one downstream steps
        used.

        Args:
            target: a node of this pipeline (the object you wired).
            **params: the ``InputParam`` values, as for ``execute``.

        Raises:
            ValueError: ``target`` is not part of this pipeline, or a required param is missing.
        """
        # `_steps` already contains every step reachable from any registered
        # target after _discover ran, so this check covers multi-target
        # pipelines built via from_targets() too.
        if target.step not in self._steps:
            raise ValueError(
                f"target step {type(target.step).__name__} is not part of this pipeline's DAG"
            )
        # Missing-param check is the same as execute() but scoped to the
        # InputParams reachable from `target`. We don't bother pruning:
        # an extra missing-param check is cheap and avoids surprises.
        missing = [p.name for p in self._inputs if p.name not in params and not p.has_default]
        if missing:
            raise ValueError(f"Missing parameters: {missing}")
        context = ResolveContext(
            cache_dir=self.cache_dir,
            store=self.store,
            params=params,
            on_progress=self.on_progress,
            observer=observer or LoggingObserver(),
            executor=executor or LocalExecutor(),
            dispatcher=dispatcher or InlineDispatcher(),
        )
        with observing(context.observer):
            return target.resolve(context)

    def save(self, path: str | Path) -> None:
        """Save all steps and the pipeline graph to a directory."""
        path = str(path)
        os.makedirs(path, exist_ok=True)

        instance_to_name: dict[int, str] = {}
        step_entries: list[str] = []

        for step in self._steps:
            name = self.step_name(step)
            instance_to_name[id(step)] = name
            step_entries.append(name)

            step_dir = os.path.join(path, name)
            # step.json is owned by Step.save_artifacts (the base writes it via
            # _serialize_step_field; SerializableStep overrides to keep the
            # legacy model_dump format that load_spec() reads). One write per
            # step, no double-write, no Pipeline.save guessing what the spec
            # should look like.
            step.save_artifacts(step_dir)

        edges: list[dict[str, str]] = []
        self._collect_edges(self.output, instance_to_name, edges)

        parameters: dict[str, dict[str, Any]] = {}
        for inp in self._inputs:
            if inp.value_type is not None:
                type_name = inp.value_type.__name__
            else:
                type_name = type(inp.default).__name__ if inp.has_default else "str"
            entry: dict[str, Any] = {"type": type_name}
            if inp.has_default:
                # Reuse the same typed-value coercion as step fields so a default
                # that's a Path / UUID / Enum / BaseModel round-trips cleanly.
                _, default_json = _serialize_step_field(inp.default)
                entry["default"] = default_json if default_json is not None else inp.default
            parameters[inp.name] = entry

        manifest: dict[str, Any] = {"steps": step_entries, "edges": edges, "parameters": parameters}
        if self.kind is not None:
            manifest["kind"] = self.kind
        if self.metadata is not None:
            manifest["metadata"] = self.metadata
        manifest["output"] = instance_to_name[id(self.output.step)]
        with atomic_writer(Path(path).joinpath("pipeline.json")) as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")

        logger.info("Pipeline saved", path=path, steps=len(step_entries))

    @classmethod
    def from_directory(
        cls,
        path: str | Path,
        step_registry: dict[str, type[Step[Any]]],
        *,
        cache_dir: Path | None = None,
    ) -> Pipeline:
        """Reconstruct a saved Pipeline from disk.

        Reads pipeline.json, loads each step via its class in `step_registry` (keyed by kind),
        replays the edge list to rebuild the DAG, and wraps the terminal StepResult.

        cache_dir defaults to None: loaded pipelines do **not** write step results
        back to disk. Pass cache_dir=Path(path) (or another writable location) when
        you want the cross-run cache for training or notebook iteration.
        """
        base = Path(path)
        with open(base.joinpath("pipeline.json")) as f:
            manifest: dict[str, Any] = json.load(f)

        kind: str | None = manifest.get("kind")
        step_entries: list[str] = list(manifest.get("steps", []))
        edges: list[dict[str, str]] = list(manifest.get("edges", []))
        parameters: dict[str, dict[str, Any]] = dict(manifest.get("parameters", {}))
        output_name: str | None = manifest.get("output")

        inputs: dict[str, InputParam[Any]] = {}
        for param_name, entry in parameters.items():
            if "default" in entry:
                inputs[param_name] = InputParam.with_default(param_name, entry["default"])
            else:
                inputs[param_name] = InputParam(name=param_name)

        loaded_steps: dict[str, Step[Any]] = {}
        for step_name in step_entries:
            step_dir = base.joinpath(step_name)
            with open(step_dir.joinpath("step.json")) as f:
                step_kind: str = json.load(f)["kind"]
            step_cls = step_registry.get(step_kind)
            if step_cls is None:
                known = ", ".join(sorted(step_registry.keys()))
                raise ValueError(f"Unknown step kind {step_kind!r} at {step_dir}. Known: [{known}]")
            loaded_steps[step_name] = step_cls.from_directory(str(step_dir))  # type: ignore[attr-defined]

        results: dict[str, StepResult[Any]] = {}
        for step_name in step_entries:
            step = loaded_steps[step_name]
            inputs_for_step: dict[str, Any] = {}
            for edge in edges:
                target = edge.get("target", "")
                if not target.startswith(f"{step_name}."):
                    continue
                input_name = target.split(".", 1)[1]
                source = edge.get("source", "")
                if source.startswith("parameter:"):
                    param_name = source.split(":", 1)[1]
                    inputs_for_step[input_name] = inputs[param_name]
                else:
                    inputs_for_step[input_name] = results[source]
            results[step_name] = step(**inputs_for_step)

        terminal_name = output_name or step_entries[-1]
        output = results[terminal_name]
        return cls(output, cache_dir=cache_dir, kind=kind, metadata=manifest.get("metadata"))

    def _collect_edges(
        self, result: StepResult[Any], instance_to_name: dict[int, str], edges: list[dict[str, str]]
    ) -> None:
        """Recursively collect edges from the StepResult graph."""
        target = instance_to_name.get(id(result.step), "?")
        for input_name, source in result.sources.items():
            if isinstance(source, StepResult):
                source_name = instance_to_name.get(id(source.step), "?")
                edge = {"source": source_name, "target": f"{target}.{input_name}"}
                if edge not in edges:
                    edges.append(edge)
                self._collect_edges(source, instance_to_name, edges)
            elif isinstance(source, InputParam):
                edge = {"source": f"parameter:{source.name}", "target": f"{target}.{input_name}"}
                if edge not in edges:
                    edges.append(edge)

    def status(self) -> None:
        """Log status of all steps."""
        for step in self._steps:
            name = self.step_name(step)
            where = self.store if self.store is not None else self.cache_dir
            if step.result_model() is not None and where is not None:
                step_result = self._find_result(step)
                if step_result and step_result.is_cached(where):
                    logger.info("step.cached", step=name)
                else:
                    logger.info("step.pending", step=name)
            else:
                logger.info("step.ready", step=name)

    def _find_result(self, target_step: Step[Any]) -> StepResult[Any] | None:
        """Find the StepResult for a given step in the graph."""
        return self._walk_find(self.output, target_step)

    def _walk_find(self, deferred: Deferred[Any], target_step: Step[Any]) -> StepResult[Any] | None:
        if isinstance(deferred, StepResult):
            if deferred.step is target_step:
                return deferred
            for source in deferred.sources.values():
                if isinstance(source, Deferred):
                    found = self._walk_find(source, target_step)
                    if found is not None:
                        return found
        return None

    def __repr__(self) -> str:
        steps = [type(s).__name__ for s in self._steps]
        inputs = [p.name for p in self._inputs]
        return f"Pipeline(steps={steps}, inputs={inputs}, cache_dir={self.cache_dir})"


def _node_levels(
    targets: Sequence[StepResult[Any]],
    stop_at: Callable[[StepResult[Any]], bool] | None = None,
) -> list[list[StepResult[Any]]]:
    """The StepResult nodes upstream of ``targets``, grouped into dependency levels.

    Kahn's algorithm over node ids. The StepResult twin of ``Pipeline.topological_levels``
    (which groups deduplicated Steps): resolution works on nodes, so it levels by ``id``. A
    node for which ``stop_at`` returns True is kept, but its upstream nodes are not walked.
    """
    upstreams: dict[int, list[StepResult[Any]]] = {}
    downstreams: defaultdict[int, list[StepResult[Any]]] = defaultdict(list)
    nodes: dict[int, StepResult[Any]] = {}
    seen: set[int] = set()
    stack: list[StepResult[Any]] = list(targets)
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        nodes[id(node)] = node
        sources: list[StepResult[Any]] = []
        if stop_at is None or not stop_at(node):
            for source in node.sources.values():
                if isinstance(source, StepResult):
                    sources.append(source)
                    downstreams[id(source)].append(node)
                    stack.append(source)
        upstreams[id(node)] = sources

    in_degree = {node_id: len(sources) for node_id, sources in upstreams.items()}
    current = [nodes[node_id] for node_id, degree in in_degree.items() if degree == 0]
    levels: list[list[StepResult[Any]]] = []
    while current:
        levels.append(current)
        following: list[StepResult[Any]] = []
        for node in current:
            for dependent in downstreams[id(node)]:
                in_degree[id(dependent)] -= 1
                if in_degree[id(dependent)] == 0:
                    following.append(dependent)
        current = following
    return levels


def _key_or_none(node: StepResult[Any], context: ResolveContext) -> str | None:
    """``node``'s cache key, or None when it has none or cannot be hashed.

    A run without a cache may pass params the key cannot encode (a database session), so a
    hashing error here only means "no key", never a failure.
    """
    try:
        return node.content_hash(params=context.params)
    except UnhashableValueError:
        return None


def _has_cache_entry(node: StepResult[Any], context: ResolveContext) -> bool:
    """Whether a saved result marker exists for ``node``. Checks files only, loads nothing."""
    key = _key_or_none(node, context)
    if key is None:
        return False
    if node.step.caches_to_path():
        return context.cache_dir is not None and context.cache_dir.joinpath(key + ".done").exists()
    store = context.result_store()
    if store is None or node.step.result_model() is None:
        return False
    return store.exists(key + ".done")


async def _pending_levels(
    targets: Sequence[StepResult[Any]], context: ResolveContext
) -> list[list[StepResult[Any]]]:
    """``_node_levels`` without the inputs of saved results.

    A store with ``runs_on = "thread"`` is asked in one store thread. The keys are computed on the
    loop first: a node's key memo is not safe to fill from two threads.
    """
    walk = partial(_node_levels, targets, lambda n: _has_cache_entry(n, context))
    store = context.result_store()
    if store is None or not _store_io.in_thread(store):
        return walk()
    for level in _node_levels(targets):
        for node in level:
            _key_or_none(node, context)
    return await _store_io.submit(walk)


async def aresolve_all(
    targets: Sequence[StepResult[Any]],
    context: ResolveContext,
    *,
    max_concurrency: int | None = None,
) -> list[Any]:
    """Resolve ``targets`` level by level on the running loop; results in ``targets`` order.

    Use it inside your own event loop (a request handler, a script) instead of sorting the graph
    and gathering ``aresolve`` per level yourself. ``context`` holds the params, observer, cache
    and the memo shared by every node.

    - Each level's nodes run together with ``asyncio.gather``; the next level starts when this
      one is done. A node with a saved result is not expanded: its upstream nodes do not run.
    - Nodes in one level with the same cache key run once.
    - ``max_concurrency`` bounds how many step bodies run at once, including the steps inside
      fan-out branches. ``None`` runs a whole level.
    - The first failure cancels the rest of its level and is raised as it is (no
      ``ExceptionGroup``).
    - Sync step bodies run on the loop and block it while they run. So does cache IO, unless the
      store sets ``runs_on = "thread"``.
    - A ``FanOutStep`` resolves its branches as tasks of this run, so they share the limit.
    - A step with ``runs_on = "thread"`` or ``"process"`` does not block the loop.

    Args:
        targets: the nodes to resolve.
        context: shared by every node; build one per run.
        max_concurrency: the most step bodies running at the same time, in any branch.
    """
    budget = context._budget
    previous = budget.semaphore
    budget.semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else previous
    try:
        return await _resolve_levels(targets, context)
    finally:
        budget.semaphore = previous


async def _resolve_levels(targets: Sequence[StepResult[Any]], context: ResolveContext) -> list[Any]:
    """The level-by-level loop of ``aresolve_all``."""
    with observing(context.observer):
        for level in await _pending_levels(targets, context):
            leaders: list[StepResult[Any]] = []
            followers: dict[int, list[StepResult[Any]]] = defaultdict(list)
            leader_by_key: dict[str, StepResult[Any]] = {}
            for node in level:
                key = _key_or_none(node, context)
                leader = leader_by_key.get(key) if key is not None else None
                if leader is None:
                    leaders.append(node)
                    if key is not None:
                        leader_by_key[key] = node
                else:
                    followers[id(leader)].append(node)
            tasks = [asyncio.create_task(node.aresolve(context)) for node in leaders]
            try:
                values = await gather_first_failure(tasks)
            finally:
                for task in tasks:
                    task.cancel()
            for leader, value in zip(leaders, values, strict=True):
                with context._lock:
                    for follower in followers[id(leader)]:
                        context._resolved[id(follower)] = (follower, value)
        return [await target.aresolve(context) for target in targets]
