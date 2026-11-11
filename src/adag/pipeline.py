"""Pipeline — wraps a Deferred graph and provides execute().

A Pipeline is constructed from a StepResult (the final output of a DAG).
Executing the pipeline resolves the full graph with parameters and caching.

    tokenizer = Tokenizer(max_length=256)
    text = InputParam("text")
    tokens = tokenizer(text=text)

    pipeline = Pipeline(tokens, cache_dir=Path("cache"))
    result = pipeline.execute(text="hello world")

When cache_dir is None (the default for from_directory), step results are
neither read from nor written to disk — every execute() runs the full DAG.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import math
import os
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from pathlib import Path
from typing import (
    Any,
    Generic,
    TypeVar,  # PEP-696 `default=` backport — stdlib typing.TypeVar rejects it on Py3.12
)

import structlog
from pydantic import BaseModel

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
    observing,
)
from adag.io import atomic_writer
from adag.item_cache import ItemCache
from adag.observers import LoggingObserver
from adag.step import Step, StepResult, _serialize_step_field

logger = structlog.get_logger()

R = TypeVar("R", bound=BaseModel)
# `Out` is the type produced by the canonical output step; `pipeline.execute()`
# returns this. Defaulting to `Any` (PEP 696) keeps unannotated callers
# unchanged — `Pipeline` without a type arg behaves exactly as before — while
# `Pipeline(my_typed_step_result)` flows the typed result into execute()'s
# return.
Out = TypeVar("Out", default=Any)


class CyclicPipelineError(ValueError):
    """Raised at construction when the Deferred graph contains a cycle (a step transitively depends on itself)."""


class UnboundInputError(ValueError):
    """Raised by Pipeline.validate() when a step has a required run() input that is neither wired nor a dataclass field."""


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
    ) -> None:
        """Wrap a Deferred graph.

        Args:
            output: the canonical terminal node of the DAG. ``execute()``
                without an argument resolves this.
            cache_dir: on-disk cache base; None disables caching.
            store: a ``CacheStore`` for step results and ``BatchStep`` item caches, used
                instead of files under ``cache_dir``.
            on_progress: optional progress callback (passed to ``ResolveContext``).
            kind: pipeline kind tag (e.g. ``"hierarchical/v1"``) used by
                ``save()`` and the pipeline registry.
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
    ) -> Pipeline[Any]:
        """Build a Pipeline that covers multiple terminal nodes.

        Real DAGs often have more than one ending point (e.g. the main
        result plus a separate audit / dedup artifact). Pass them all
        here and the Pipeline discovers the union of their sub-graphs.
        The first target is treated as the canonical ``output`` so a
        zero-arg ``execute()`` still has a single thing to return —
        downstream callers use ``resolve(target)`` to pick any of them.
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
        )

    @property
    def targets(self) -> list[StepResult[Any]]:
        """All terminal / target nodes the pipeline was built to resolve."""
        return list(self._targets)

    def _discover(self, deferred: Deferred[Any], visiting: set[int] | None = None) -> None:
        """Walk the Deferred graph backward to collect all steps and parameters.

        ``visiting`` is the current DFS path (StepResult ids). Re-entering a node already
        on the path means a cycle — we raise CyclicPipelineError instead of letting the
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
        """All steps in dependency (topological) order — every step appears after the steps
        it depends on, guaranteed by the post-order discovery walk in ``_discover``.
        """
        return list(self._steps)

    def topological_order(self) -> list[Step[Any]]:
        """Steps in a guaranteed topological order (each step after its dependencies).

        A named contract (delegates to ``steps``) so consumers that depend on the ordering
        — deterministic execution, left-to-right layered layout, level grouping — rely on
        this rather than on the ``steps`` discovery detail.
        """
        return self.steps

    def topological_levels(self) -> list[list[Step[Any]]]:
        """Steps grouped into dependency levels: every step lands one level past its deepest
        dependency. Steps within a level are mutually independent, so a concurrent executor
        can run a whole level in parallel; level i+1 starts once level i is resolved.

        Pure analysis — no execution change. The foundation for opt-in parallel execution
        (run each level's steps together) and for correct left-to-right layered layout.
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
        """The graph's StepResult nodes grouped into dependency levels (Kahn over node ids).

        The StepResult-identity twin of ``topological_levels`` (which groups deduped Steps):
        the concurrent executor resolves nodes, not steps, so it levels by ``id``.
        """
        upstreams: dict[int, list[StepResult[Any]]] = {}
        downstreams: defaultdict[int, list[StepResult[Any]]] = defaultdict(list)
        nodes: dict[int, StepResult[Any]] = {}
        seen: set[int] = set()
        stack: list[StepResult[Any]] = list(self._targets)
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            nodes[id(node)] = node
            sources: list[StepResult[Any]] = []
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

    def _resolve_levels_concurrently(
        self, context: ResolveContext, max_workers: int | None
    ) -> None:
        """Resolve the whole graph level by level, each level's nodes in a thread pool.

        Steps within a level are independent, so they run concurrently; the next level
        starts only once this one is fully resolved. ``resolve`` memoises by ``id`` and
        every node's upstreams are resolved in earlier levels, so each node finds its inputs
        already in ``context._resolved`` and just runs its own step — no recursion, no step
        run twice.

        Each StepResult is resolved exactly once (in its level), with upstream keys written
        in earlier levels and only read here. ``context._lock`` (see ResolveContext) guards
        every ``_resolved`` read/write, so concurrent nodes are safe regardless of the GIL —
        not reliant on CPython dict-op atomicity, which the free-threaded build removes.

        Each node runs inside a COPIED context (``copy_context().run``) so ambient ContextVars —
        the usage observer, ``current_observer`` — propagate into the level workers, which a raw
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

    def validate(self) -> None:
        """Validate the graph before ``execute()``: every step's required run() input is bound.

        Cycles are already rejected at construction (see ``_discover``). This adds the
        missing-input check: a required parameter of a step's ``run()`` that is neither a
        wired source nor one of the step's dataclass fields would otherwise fail late at
        ``execute()`` as a bare ``TypeError: run() missing 1 required argument``. Opt-in
        (not run at construction) so a consumer building a graph from dynamic/untrusted
        input can validate it up front and reject with a 4xx instead of crashing
        mid-resolution. Raises ``UnboundInputError`` naming the step and the unbound params.

        Walks the target StepResults directly (visited by id — the same node identity
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

    def _check_inputs_bound(self, node: StepResult[Any]) -> None:
        """Raise UnboundInputError if ``node.step`` has a required ``run()`` parameter that is
        neither a wired source nor one of its dataclass fields — both feed ``run()`` (see
        ``StepResult._merge_step_defaults``), so either satisfies the parameter.
        """
        field_names = {field.name for field in dataclasses.fields(node.step)}
        available = set(node.sources.keys()) | field_names
        try:
            signature = inspect.signature(type(node.step).run)
        except (ValueError, TypeError):
            return
        required: set[str] = set()
        for name, parameter in signature.parameters.items():
            if name == "self" or parameter.kind in (
                inspect.Parameter.VAR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            ):
                continue
            if parameter.default is inspect.Parameter.empty:
                required.add(name)
        missing = required - available
        if missing:
            label = type(node.step).label or type(node.step).__name__
            raise UnboundInputError(
                f"Step {label!r} has unbound required input(s): {sorted(missing)}"
            )

    @property
    def input_names(self) -> set[str]:
        """Names of every InputParam this pipeline expects from execute(**kwargs).

        Public alternative to reaching into ``_inputs`` from outside the package
        — callers that branch on "does this pipeline accept a `threshold` kwarg"
        should use this so the private list shape stays free to evolve.
        """
        return {p.name for p in self._inputs}

    @property
    def inputs(self) -> list[InputParam[Any]]:
        """Every InputParam this pipeline expects, in discovery order.

        Public accessor for callers (e.g. the CLI) that need each param's name,
        type, and default — not just the names from ``input_names`` — to build
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
        """Execute the full DAG with parameters.

        ``observer`` is an optional instrumentation hook (see
        ``PipelineObserver``). Defaulting to ``None`` materialises a
        ``LoggingObserver`` (per-step lifecycle logging); pass ``NullObserver``
        for silence. Reserved as a kwarg here — pipelines must not define an
        ``InputParam`` named ``observer``.

        ``executor`` decides *where* each step runs (see ``Executor``).
        Defaulting to ``None`` materialises a ``LocalExecutor`` — every step
        runs in-process, exactly as before. Also reserved.

        ``dispatcher`` decides *how* a fan-out branch is resolved (see
        ``BranchDispatcher``). Defaulting to ``None`` materialises an
        ``InlineDispatcher`` — branches resolve in-process, exactly as before.
        Pass a ``SubprocessDispatcher`` to run branches in worker processes, or a
        ``RemoteDispatcher`` to run them on a job scheduler. Also reserved.

        ``parallel`` runs each topological level's steps concurrently in a
        thread pool (``max_workers``, default one per level) — a win for
        I/O-bound DAGs (DuckDB, LLM, HTTP steps). Independent steps within a
        level run together; level i+1 starts once level i is resolved. Default
        ``False`` keeps the sequential recursive path. ``RunTreeObserver`` is
        concurrency-safe (per-thread stacks); under ``parallel`` its tree is
        flat because level-scheduled execution has no recursion to nest.
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
            executor=executor or LocalExecutor(),
            dispatcher=dispatcher or InlineDispatcher(),
        )
        with observing(context.observer):
            if parallel:
                self._resolve_levels_concurrently(context, max_workers)
            return self.output.resolve(context)

    async def aexecute(
        self,
        *,
        observer: PipelineObserver | None = None,
        async_executor: AsyncExecutor | None = None,
        **params: Any,
    ) -> Out:
        """Async mirror of ``execute`` — resolve the full DAG, awaiting async step bodies on the event loop.

        Use this when steps await I/O (a DB ``AsyncSession``, an HTTP/LLM call): each ``AsyncStep`` runs ON
        the loop (no thread offload, so the loop stays responsive), sync steps run inline, and cancelling
        the awaiting task unwinds the resolution without starting further steps. ``cache_dir=None`` (the
        Pipeline default) keeps every run cache-free — the right mode for per-request resolution.

        ``observer`` / ``async_executor`` are reserved kwargs (a pipeline must not define ``InputParam``s by
        those names), mirroring ``execute``'s ``observer`` / ``executor``. There is no ``parallel`` flag:
        async concurrency is the event loop's job, and the default ``aresolve`` is sequential so a single
        shared per-request resource (one ``AsyncSession``) is never used concurrently.
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
        with observing(context.observer):
            return await self.output.aresolve(context)

    def resolve(
        self,
        target: StepResult[R],
        *,
        observer: PipelineObserver | None = None,
        executor: Executor | None = None,
        dispatcher: BranchDispatcher | None = None,
        **params: Any,
    ) -> R:
        """Resolve an intermediate node of this pipeline's DAG.

        Use this when you already executed the full pipeline and want a
        cached intermediate result (e.g. the schedule produced by an
        upstream ``SampleEvents`` step) without re-implementing the
        Deferred wiring or calling ``step.run(...)`` directly. Both of
        those alternatives skip the pipeline cache and can return a
        *different* output than the one downstream steps consumed —
        silently corrupting cross-step consistency when step code has
        changed since the cache was written.

        Why this matters: the per-step content_hash invalidates a step's
        cache when its inputs change. But if you call ``step.run()``
        outside the pipeline, you're computing a fresh result with the
        *current* code against the *current* inputs, which can disagree
        with what the cached pipeline used. This method routes through
        the same ``StepResult.resolve`` path the pipeline uses so the
        cached result is returned verbatim.

        Args:
            target: the ``StepResult`` of the intermediate node you want
                (the same object you wired into ``Pipeline(...)``).
            **params: any ``InputParam`` values the sub-DAG depends on,
                same shape as ``execute(**params)``.

        Returns:
            The resolved result of ``target``, cache-hit when available.

        Raises:
            ValueError: if ``target`` is not part of this pipeline's DAG.
        """
        # `_steps` already contains every step reachable from any registered
        # target after _discover ran, so this check covers multi-target
        # pipelines built via from_targets() too.
        if target.step not in self._steps:
            raise ValueError(
                f"target step {type(target.step).__name__} is not part of this pipeline's DAG"
            )
        # Missing-param check is the same as execute() but scoped to the
        # InputParams reachable from `target`. We don't bother pruning —
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

        cache_dir defaults to None — loaded pipelines do **not** write step results
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
        return cls(output, cache_dir=cache_dir, kind=kind)

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
