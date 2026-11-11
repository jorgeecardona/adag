# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `SerializableStep.from_directory` has a default that rebuilds a step from its `spec`. A step
  it cannot rebuild now raises `TypeError` at save time instead of failing at load time.
- `AdagDeprecationWarning`: every deprecated name and call form warns with it. It is a
  `FutureWarning`, so scripts and notebooks see it without changing warning filters, and each
  message names the replacement. Deprecated forms keep working until adag 1.0.
- Python 3.12 support (`requires-python >=3.12`), tested in CI.
- `Pipeline(metadata=...)`: caller data, any JSON value, saved in `pipeline.json` and read back
  by `from_directory`.
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

- A `FanOutStep` runs under `Pipeline.aexecute()`. It raised `NotImplementedError` before.

### Changed

- Calling a step warns (`DeprecationWarning`) when an upstream step is both a field and an
  input. Wire it once, as an input. It becomes a `TypeError` in 0.4.
- `SubprocessDispatcher` runs at most one process per CPU at a time (`max_concurrent=`), not
  one per branch. It waits `retry_delay_seconds`, not `poll_interval_seconds`, between attempts.
- A custom dispatcher implements `dispatch_all(branches, context)` (all branches of one fan-out)
  instead of `dispatch(branch, sub, context)`.
- Dispatching a fan-out whose keys have the same text (`1` and `"1"`) raises `ValueError`.
- `NullObserver` now silences a whole run. Progress logs (cache writes, batch progress,
  dispatch) go through the new `on_event` observer hook; warnings and errors still log.

### Removed

- `Step.produces`, `Step.accepts` and `Step.accepts_from`. Nothing checked them.
- `BranchExecutionError`. Branch failures arrive as `FanOutBranchError`.

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
