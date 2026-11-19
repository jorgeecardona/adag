# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.7] - 2026-11-19

### Added

- A `FanOutStep` can fan out over the result of an upstream step. Give it a `Template` and wire
  `over`: `FanOutStep(template=t)(over=split)`. The keys are known when the run reaches the
  fan-out, so a `Split` step can decide how many branches there are.
- Each branch is cached under the key of the sub-graph built for its item. When an upstream step
  runs again and returns the same items, every branch is a cache hit. A changed item recomputes
  only its branch.
- A fan-out that uses a `template` runs in this process (sequential or `max_workers`). With a
  dispatcher it raises `NotImplementedError`: dispatching these branches comes later.

### Changed

- `FanOutStep(over=..., factory=...)` keeps its keys. A test pins them.
- `FanOutStep` raises `TypeError` when you give neither or both of `factory` and `template`.

## [0.3.6] - 2026-11-19

### Added

- `Template(build=fn, inputs=("key", "item"))`: a sub-graph with named inputs. adag builds it once
  with placeholders to get its hash, so the hash does not depend on any item. A `build` that reads
  the value of an input raises `TemplateInputError`. `FanOutStep` takes a template in 0.3.7.

## [0.3.5] - 2026-11-14

### Changed

- The PyPI page and README start with what adag does: the cache key and what reruns after an edit.

## [0.3.4] - 2026-11-14

### Changed

- The Issues link on PyPI and in the docs points to `/-/work_items`, which loads when you are
  logged out. The old `/-/issues` URL returned 404.
- The key record `.keys/<key>.json` no longer holds the value of a skipped component, such as a
  `Config` field. A `Config` can hold a client or a secret, and the record goes to disk or to a
  shared store. The record keeps the field name.
- `dag()` now draws cached steps green, as the docs always said. Before, a cached step and a
  pending step had the same fill, and only blocked steps had their own color. The classes
  `is-cached`, `is-pending` and `is-blocked` are unchanged, so your own stylesheet still wins.

## [0.3.0] - 2026-11-12

0.2.6 to 0.2.43 were published per commit; this entry collects all of them. Read
[Migrating to 0.3.0](https://cardona-oss.gitlab.io/adag/migration/) before you upgrade: every
step result recomputes once, and so do `BatchStep` items whose keys adag derives.

### Added

- Custom graph drawing has a public API: `node_metadata(nodes)` returns what `dag` would draw;
  replace any `NodeMetadata` and pass the list to `render_graph_svg`. `Node`, `Edge`,
  `NodeMetadata`, `compute_state`, `node_metadata` and `render_graph_svg` import from `adag`.
  The old private names keep working until 1.0.
- `Pipeline(metadata=...)`: caller data, any JSON value, saved in `pipeline.json` and read back
  by `from_directory`.
- `adag.JsonValue`: the type of `Pipeline(metadata=...)` and `pipeline.metadata`, instead of
  `Any`. A strict type checker now asks you to narrow the loaded value before you use it.
- `aresolve_all(targets, context, max_concurrency=)`: resolve several nodes level by level inside
  your own event loop, with one memo, equal keys run once, and a bound on running step bodies.
  `Pipeline.aexecute` now uses it.
- After each save, adag writes a key record `.keys/<key>.json` (the `explain_key` output, under
  1 KB for a small step) next to the result. Code that lists every file in a cache directory
  now also sees `.keys/`.
- `StepResult.explain_key(params)` and `Pipeline.explain_keys(params)` list every component of
  a cache key, so you can see why a node recomputed. See the docs page "Why a step recomputed".
- `adag.hashing.KEY_FORMAT` (now 2): the version of the cache key bytes. A release that changes
  every key bumps it.
- `SerializableStep.from_directory` has a default that rebuilds a step from its `spec`. A step
  it cannot rebuild now raises `TypeError` at save time instead of failing at load time.
- `AdagDeprecationWarning`: every deprecated name and call form warns with it. It is a
  `FutureWarning`, so scripts and notebooks see it without changing warning filters, and each
  message names the replacement. Deprecated forms keep working until adag 1.0.
- Python 3.12 support (`requires-python >=3.12`), tested in CI.
- `Step.save_result` and `Step.load_result`: cache a result that is not one JSON document
  (JSONL, a directory, a file plus a sidecar), under a path keyed on the step and its inputs.
  `BatchStep` uses them too.
- `InputParam(..., value_type=T)` tells `adag run` how to parse `--param`, so a required `str`
  param keeps `007` as `"007"`.
- `RemoteDispatcher(backend)` runs fan-out branches as jobs on any scheduler. A backend
  (`JobBackend`) has three methods: `submit`, `status`, `cancel`. `LocalJobBackend` runs local
  processes.
- `BranchPolicy` gains `queue_timeout_seconds`, `retry_delay_seconds` and
  `result_grace_seconds`. `timeout_seconds` now counts from when the job starts running.
- `Pipeline(..., store=CacheStore)` keeps step results and `BatchStep` item caches in SQLite,
  SQL, Redis or memory instead of files under `cache_dir`.
- `store_from_url(url)` opens a store from a URL, and `adag resolve-branch --store-url` lets a
  worker write to that store. Pass `store_url=` to `RemoteDispatcher` when the pipeline has a
  store.
- `adag resolve-branch` takes a module name (`pkg.pipeline`) as well as a file path, and
  `--expect-hash`. A worker whose branch hash differs exits 2 and is not retried.

### Fixed

- The per-run memo keeps each node with its result. A `ResolveContext` reused after its graph
  was freed could hand one node's result to a new node that got the same `id()`.
- Two `aresolve` calls gathered on one context ran a shared upstream step twice. The first task
  now claims the node and the others wait for it, as the threaded path already did.
- `dag(..., cache_dir=)` draws pipeline-cached steps as cached. It read the step hash, which the
  pipeline does not use as a key, so every step showed as pending.
- A `FanOutStep` runs under `Pipeline.aexecute()`. It raised `NotImplementedError` before.
- A `FileSource`, `LinesSource` or `JSONLSource` on a directory (an empty path included) raises
  an error that names the source and the path. It raised a bare `IsADirectoryError` from inside
  hashing.
- `StepResult.is_cached` for a step with `save_result`/`load_result` calls `load_result`, so an
  entry it rejects is not cached, the same answer the resolver gives. It checked only that the
  files existed.
- A step whose `load_result` gives back less than `run` produced (for example a field marked
  `exclude=True`) now logs `step.load_result_differs` on the first save. Downstream gets the
  reloaded value, so the data was dropped silently. The Caching page now says so.
- A `StepResult` held in a step field raises `TypeError` when the step is called, and says to
  pass it as a call input. It failed later, in hashing, with advice to use `Config(...)`, which
  would have removed the edge from the graph. Hashing errors no longer suggest `Config(...)` for
  a graph node.

### Changed

- Tests pin the cache key format. A future release changes keys only on purpose, and says
  so here.
- Calling a step warns (`AdagDeprecationWarning`) when an upstream step is both a field and an
  input. Wire it once, as an input. It becomes a `TypeError` in adag 1.0.
- `SubprocessDispatcher` runs at most one process per CPU at a time (`max_concurrent=`), not
  one per branch. It waits `retry_delay_seconds`, not `poll_interval_seconds`, between attempts.
- A custom dispatcher implements `dispatch_all(branches, context)` (all branches of one fan-out).
  A dispatcher with only the old `dispatch(branch, sub, context)` still works, with a warning.
- Dispatching a fan-out whose keys have the same text (`1` and `"1"`) raises `ValueError`.
- `NullObserver` now silences a whole run. Progress logs (cache writes, batch progress,
  dispatch) go through the new `on_event` observer hook; warnings and errors still log.

### Restored

The 0.2 releases removed names that existing code still uses. They are back, and every name
the first release exported imports again, except `BatchDispatcher` (use `RemoteDispatcher` with a
`JobBackend`) and `StepOutput` (nothing returned one). Deprecated ones warn and work until 1.0.

- The top-level exports removed in 0.2.5: executors, dispatchers, `Deferred`, `ResolveContext`,
  `Progress`, `BranchRef`, `current_observer`, `observing`, `walk`, `render_pipeline_svg`.
- `Step.produces`, `Step.accepts` and `Step.accepts_from`.
- `CachedStep` as a working class, with its direct `output_path`, `save`, `load`, `validate`,
  `is_cached` and `is_partial` methods (deprecated; see `save_result`/`load_result`).
- `hash_scalar` and `hash_list`, with the same bytes as the first release.
- `ResolveContext` and `BranchPolicy` keep their first-release positional field order.

### Deprecated

Each warns with `AdagDeprecationWarning` and works until adag 1.0.

- `CachedStep`: subclass `Step`, and override `save_result`/`load_result` for a result that is
  not one JSON document.
- `Parameter`, `StepMode`, `BranchExecutionError`.
- `NullObserver.on_embedding` and `on_model_refused`: declare a typed hook of your own.
- A dispatcher with only `dispatch()`: implement `dispatch_all`.
- `run_branch` with a `KEY=VALUE` list, and `resolve-branch --param`: use `--params-json`.
- An upstream step that is both a field and an input.

### Removed

- `BatchDispatcher`: use `RemoteDispatcher` with a `JobBackend`.
- `StepOutput`: nothing returned one.

## [0.2.5] - 2026-11-11

0.2.0 to 0.2.5 are one change set; patch versions were bumped per commit.

### Changed

- All caches recompute once: the cache key format changed to fix the bugs below.
- `CachedStep` is deprecated: subclass `Step` instead. Its direct `save`, `load`,
  `is_partial` and `output_path` methods are removed; the pipeline caches every `Step`.
- A step field the cache key cannot hash now raises `UnhashableValueError`. Wrap
  execution-only values in `Config(...)`, or set `strict_content_hash = False`.

### Fixed

- Lists with the same digits in a different split, like `(1, 23)` and `(12, 3)`, no longer
  share a cache key.
- Changing a `Path`, `Enum`, `dict`, `set` or `UUID` field now changes the cache key.
- `InputParam` values hash by value, not by `repr()`, so they get cache hits.
- An empty or corrupt cache file is recomputed instead of crashing.
- Threaded fan-out results keep the order of `over`.
- Worker processes receive params as JSON, so a str `"5"` stays a str.
- Steps with a `kind` hash their source code too, so a code edit recomputes.

### Removed

- From the top-level `adag` namespace: executors, dispatchers, `Deferred`, `ResolveContext`,
  `Progress`, `BranchRef`, `current_observer`, `observing`, `walk`, `render_pipeline_svg`.
  Import them from their submodules (for example `adag.dispatch`).
- `Parameter`, `StepMode`, `StepOutput`, `BatchDispatcher`, and the `on_embedding` and
  `on_model_refused` observer hooks.

## [0.1.1] - 2026-11-11

### Added

- Initial release.
