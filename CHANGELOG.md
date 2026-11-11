# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-11

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

### Removed

- From the top-level `adag` namespace: executors, dispatchers, `Deferred`, `ResolveContext`,
  `Progress`, `BranchRef`, `current_observer`, `observing`, `walk`, `render_pipeline_svg`.
  Import them from their submodules (for example `adag.dispatch`).
- `Parameter`, `StepMode`, `StepOutput`, `BatchDispatcher`, and the `on_embedding` and
  `on_model_refused` observer hooks.

## [0.1.1] - 2026-11-11

### Added

- Initial release.
