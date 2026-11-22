"""BatchStep: a step that processes items one at a time, with a cache entry per item.

Use it for expensive work (model calls, API requests) where each item costs money or time,
a crash at item 1500 of 2000 must not lose the first 1500, and a restart must skip the items
already done. adag partitions the items into cached and uncached, saves each result as it
completes, and marks the step ``.done`` only when all items are done.

A subclass picks one of two patterns.

**Pattern A (recommended): ``items_data()`` and derived keys.**

adag derives each item key from the step's own hash, the item data and the other inputs of
the step. The subclass declares what to process and never builds keys::

    @dataclass(frozen=True)
    class Greet(BatchStep[Greetings, Greeting]):
        items_source_keys = ("names",)

        def items_data(self, names: list[str]) -> list[str]:
            return names

        def process_item(self, item: BatchItem, names: list[str]) -> Greeting:
            return Greeting(text=f"hello {item.data}")

        def assemble(self, results: list[Greeting], names: list[str]) -> Greetings:
            return Greetings(items=results)

    greetings = Greet()(names=names_param)

When a field or an upstream output changes, the keys change too, so a key cannot go stale.

**Pattern B: ``items()`` for full control.**

To share a cache between slightly different inputs, override ``items()`` and build
``BatchItem.key`` yourself. The default ``items()`` calls ``items_data()`` and wraps the
derived keys.
"""

from __future__ import annotations

import hashlib
import json
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar, get_args, get_origin

import structlog
from pydantic import BaseModel

from adag.deferred import ResolveContext, emit
from adag.item_cache import ItemCache
from adag.step import Step

logger = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)  # final result type
R = TypeVar("R", bound=BaseModel)  # per-item result type


@dataclass(frozen=True)
class BatchItem:
    """A single work unit in a BatchStep.

    key:  cache key for this item (determines cache identity)
    data: arbitrary payload passed to process_item()
    """

    key: str
    data: Any = None


class BatchStep(Step[T], Generic[T, R]):
    """A step that processes items individually with per-item disk caching.

    Subclasses implement three methods:

    1. ``items(**inputs)``: declare work units and their cache keys
    2. ``process_item(item, **inputs)``: handle one item
    3. ``assemble(results, **inputs)``: combine all results into final output

    The framework automatically:
    - Partitions items into cached/uncached
    - Skips already-processed items
    - Saves each result to disk immediately after processing
    - Logs progress (cached/total, processing each item)
    - Only marks .done when ALL items are complete

    On crash: restart picks up where it left off, only uncached items run.

    **Per-item cache key derivation.** The framework auto-derives each
    item's cache key from ``sha256(step_signature + inputs_signature +
    item_data)``. The inputs signature includes every kwarg passed to
    :meth:`execute` except those listed in :attr:`items_source_keys`.
    Those are treated as containers of items rather than shared
    parameters. Subclasses that take a large input list and extract items
    from it should set ``items_source_keys`` to the kwarg name(s) so that
    adding or removing items doesn't invalidate every other item's cache::

        class Greet(BatchStep[Greetings, Greeting]):
            items_source_keys = ("names",)  # the list of items
            # An input that is not listed here (a prompt, for example) is shared by
            # every item, so a change to it invalidates all per-item caches.
    """

    item_type: type[BaseModel]  # auto-resolved from BatchStep[T, R]

    # Names of input kwargs that are CONTAINERS of items rather than
    # shared parameters. Excluded from per-item cache key derivation.
    # Default empty: ALL inputs participate in the key (safe but
    # potentially over-invalidating). Subclasses with a large source-list
    # should override.
    items_source_keys: ClassVar[tuple[str, ...]] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for parent in getattr(cls, "__orig_bases__", []):
            origin = get_origin(parent)
            if origin is BatchStep:
                args = get_args(parent)
                if len(args) >= 2:  # noqa: PLR2004  (BatchStep[T, Item]: the item type is the 2nd arg)
                    cls.item_type = args[1]
                    return

    def items_data(self, *args: Any, **inputs: Any) -> list[Any]:
        """Declare the per-item DATA only, framework derives the cache key.

        Recommended override path. The default ``items()`` (below) calls
        this and wraps each entry in a ``BatchItem`` with an auto-computed
        key that includes the step's content_hash + the item's repr.

        Returns the list of data objects (any type) that
        ``process_item(item, ...)`` will receive as ``item.data``.

        Override this method when you don't need custom key topology.
        Then no cache-poisoning bug from manual key composition is possible.
        The base signature is ``*args: Any, **inputs: Any`` so pyright accepts an override with
        named parameters. adag calls the hook with keywords only.
        """
        del args, inputs
        raise NotImplementedError(
            f"{type(self).__name__} must override either `items_data()` (framework derives keys) or `items()` (full control)."
        )

    def items(self, *args: Any, **inputs: Any) -> list[BatchItem]:
        """Declare the work units. Called once at the start.

        Default implementation calls ``items_data(**inputs)`` and assigns
        each item an auto-generated cache key derived from
        ``(step.content_hash, **inputs, item_data)``. The inputs signature
        is included so that step kwargs which AFFECT the per-item LLM call
        (e.g. a notes payload passed alongside the report shortlists)
        invalidate item caches when they change, even though they are
        not step dataclass fields.

        Override this only when you need custom key topology (e.g.
        deliberately sharing a cached result across two slightly-different
        inputs). The base signature is ``*args: Any, **inputs: Any``; adag passes keywords only.
        """
        del args
        step_sig = self._step_signature()
        inputs_sig = self._inputs_signature(inputs)
        return [
            BatchItem(key=self._auto_item_key(step_sig, datum, inputs_sig), data=datum)
            for datum in self.items_data(**inputs)
        ]

    def _step_signature(self) -> str:
        """A short hex digest of the step's identity for use in item keys.

        Falls back to the class kind only if ``content_hash()`` is
        blocked by an unsatisfied upstream (e.g. a missing FileSource).
        Whenever the step's output semantics change, ``content_hash()``
        changes, and so does every item's auto-generated cache key.
        """
        return self.content_hash() or type(self).kind or type(self).__name__

    def _inputs_signature(self, inputs: dict[str, Any]) -> str:
        """Hash all BaseModel/list/scalar inputs into a single digest.

        The point of this hash is to catch upstream-result changes (like a
        notes payload, a scoped item list, a config blob) that affect
        what ``process_item`` produces but are not step dataclass fields.
        Without this, a BatchStep's per-item cache would silently return
        stale results whenever an input kwarg changed but step config and
        per-item data did not.

        Inputs named in :attr:`items_source_keys` are skipped. They are
        containers from which items are extracted, not shared parameters.
        Including them would invalidate every item's cache when the list
        of items changed (e.g. one new report added).
        """
        skip = set(type(self).items_source_keys)
        h = hashlib.sha256()
        for name in sorted(inputs.keys()):
            if name in skip:
                continue
            val = inputs[name]
            h.update(name.encode())
            h.update(b"=")
            if isinstance(val, BaseModel):
                h.update(val.model_dump_json().encode())
            elif isinstance(val, (str, int, float, bool)) or val is None:
                h.update(repr(val).encode())
            elif isinstance(val, (list, tuple)):
                try:
                    h.update(json.dumps(val, sort_keys=True, default=str).encode())
                except (TypeError, ValueError):
                    # repr() handles any object; pyright over-narrows the
                    # generic params here because dict was dict[str, Any].
                    h.update(repr(val).encode())  # pyright: ignore[reportUnknownArgumentType]
            else:
                # Unknown type: fall back to repr for stability.
                h.update(repr(val).encode())
            h.update(b"|")
        return h.hexdigest()[:16]

    def _auto_item_key(self, step_sig: str, item_data: Any, inputs_sig: str = "") -> str:
        """Derive a stable cache key from ``(step_sig, inputs_sig, item_data)``.

        ``item_data`` can be any object. We serialize it deterministically
        (Pydantic JSON if possible, then a stable repr fallback). The
        digest is truncated to 16 hex chars to match ItemCache.key().

        ``inputs_sig`` is the digest of all ``**inputs`` kwargs from
        :meth:`execute`. Included so that changing any input invalidates
        per-item caches: see :meth:`_inputs_signature`.
        """
        h = hashlib.sha256()
        h.update(step_sig.encode())
        h.update(b"|")
        h.update(inputs_sig.encode())
        h.update(b"|")
        if isinstance(item_data, BaseModel):
            h.update(item_data.model_dump_json().encode())
        else:
            try:
                h.update(json.dumps(item_data, sort_keys=True, default=str).encode())
            except (TypeError, ValueError):
                h.update(repr(item_data).encode())
        return h.hexdigest()[:16]

    @abstractmethod
    def process_item(self, item: BatchItem, *args: Any, **inputs: Any) -> R:
        """Process a single item. Default implementation called by process_batch().

        Override this for simple one-at-a-time processing.
        Receives the BatchItem plus all loaded input kwargs.
        """
        ...

    def process_batch(self, items: list[BatchItem], cache: ItemCache[R], **inputs: Any) -> None:
        """Process a batch of uncached items, saving each result to cache.

        Override this to control batching strategy (concurrent LLM calls,
        chunked API requests, etc.). The default processes one at a time.

        Each result MUST be saved to cache immediately via ``cache.save()``
        so a crash loses at most one in-flight item.
        """
        for i, item in enumerate(items, start=1):
            emit(
                "batch.processing",
                step=type(self).__name__,
                item=item.key[:12],
                progress=f"{i}/{len(items)}",
            )
            # WAL: write the `.inflight` sentinel before the call starts.
            # If `process_item` raises or the process is killed, the
            # marker survives so the next resume can surface "you lost
            # work on these items". `cache.save()` clears it on success.
            cache.mark_inflight(item.key)
            result = self.process_item(item, **inputs)
            cache.save(item.key, result)

    @abstractmethod
    def assemble(self, results: list[R], *args: Any, **inputs: Any) -> T:
        """Combine all per-item results into the final step result.

        Called after all items (cached + freshly processed) are available.
        Receives the ordered list of results plus all loaded input kwargs.
        """
        ...

    def item_cache_namespace(self) -> str:
        """Namespace for the ItemCache. Default: class name in snake_case."""
        name = type(self).__name__
        result: list[str] = []
        for i, c in enumerate(name):
            if c.isupper() and i > 0:
                result.append("_")
            result.append(c.lower())
        return "".join(result)

    def run(self, **inputs: Any) -> T:  # pyright: ignore[reportUnusedParameter]
        """Raise: a BatchStep is driven through ``execute()``, never ``run()``."""
        raise RuntimeError("BatchStep uses execute(context, ...); do not call run() directly.")

    def execute(self, context: ResolveContext, /, **inputs: Any) -> T:
        """Orchestrate: items → partition → process uncached → assemble.

        Called by StepResult.resolve with the current ResolveContext so the
        per-item cache can live under the pipeline's cache directory.
        Subclasses implement items/process_item/assemble, not this.
        """
        if context.cache_dir is None and context.store is None:
            raise RuntimeError(
                f"{type(self).__name__} requires a Pipeline cache_dir or store for per-item caching."
            )
        cache_dir = context.cache_dir or Path()  # unused when the store is set

        namespace = self.item_cache_namespace()
        item_schema: Any = self.item_type
        cache: ItemCache[R] = ItemCache(cache_dir, namespace, item_schema, store=context.store)

        # Step 1: Declare items
        all_items = self.items(**inputs)
        keys = [item.key for item in all_items]

        emit(
            "batch.start",
            step=type(self).__name__,
            total=len(all_items),
            namespace=namespace,
        )

        # Step 2: Partition cached/uncached
        _cached, uncached_indices = cache.partition(keys)
        n_cached = len(all_items) - len(uncached_indices)

        if n_cached > 0:
            emit(
                "batch.resuming",
                step=type(self).__name__,
                cached=n_cached,
                remaining=len(uncached_indices),
            )

        # Step 3: Process uncached items via process_batch()
        # The step decides internally how to batch (concurrent, chunked, etc.)
        uncached_items = [all_items[idx] for idx in uncached_indices]
        if uncached_items:
            self.process_batch(uncached_items, cache, **inputs)

        # Step 4: Collect all results in order.
        # Tolerate per-item failures: LLM batches in particular routinely have
        # a small percentage fail due to rate limits / transient errors, and
        # the successful majority is worth keeping. Subclasses that need
        # strict behavior should validate the assembled result themselves.
        all_results = cache.collect(keys, allow_missing=True)

        emit(
            "batch.done",
            step=type(self).__name__,
            total=len(all_results),
            processed=len(uncached_indices),
            from_cache=n_cached,
        )

        # Step 5: Assemble final result
        return self.assemble(all_results, **inputs)

    def run_concurrent(self, *, cache_dir: Any, **inputs: Any) -> T:
        """Drive a BatchStep outside a Pipeline.resolve() context.

        Use this when another Step's ``run()`` body needs to invoke a
        BatchStep over derived data (e.g. running Stage-2 classification
        from inside a larger Step). This helper builds a minimal
        ResolveContext rooted at ``cache_dir`` and dispatches through
        :meth:`execute`, which guarantees:

        * Item cache keys are auto-derived from
          ``sha256(step.content_hash() + item.model_dump_json())[:16]``,
          so any change to step configuration (model, max_candidates,
          notes, etc.) OR to a single item's input invalidates that
          item's cache entry. No stale results.
        * Partition / process_batch / collect / assemble run in order.

        DO NOT build ``BatchItem(key=..., data=...)`` manually and call
        ``process_batch(items, cache, **inputs)`` directly. That bypasses
        the framework's content-aware key derivation. Custom keys that
        encode only part of the inputs (e.g. external_id but not the
        actual data being sent to a model) silently return stale results
        when the un-encoded inputs change. ``run_concurrent`` exists
        specifically to make this footgun harder to find.
        """
        from adag.deferred import ResolveContext

        context = ResolveContext(cache_dir=cache_dir, params={}, on_progress=None)
        return self.execute(context, **inputs)
