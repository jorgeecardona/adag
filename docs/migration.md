# Migrating to 0.3.0

Read this before you upgrade code written for the first release (0.1) or for 0.2.x. Each section
says what changed and what to edit.

## Pin a version first

Pin adag in your project, for example `adag>=0.3,<0.4`. Without a pin, your project installs
every release, including the one that removes a deprecated name.

## Caches recompute once

The cache key format changed in 0.2 (`adag.hashing.KEY_FORMAT` is now 2). On the first run after
the upgrade:

- Every step result recomputes.
- Every `BatchStep` item whose key adag derives (`items_data`) recomputes. If those items call a
  paid API, budget for one more full pass.
- Items you saved under keys you built yourself with `ItemCache.key(...)` survive: that function
  and the item file layout did not change.

To see what goes into a key, call `step_result.explain_key(params)`. See
[Caching](caching.md#why-did-a-node-recompute).

## CachedStep is deprecated

`CachedStep` still works, and subclassing it warns. A subclass that only defines `run` needs one
edit: subclass `Step` instead.

A subclass that overrides `save`, `load` and `validate` moves to `save_result` and `load_result`.
adag then picks the path from the step, its inputs and its params, so two runs with different
inputs no longer share one file. `load_result` raising replaces `validate` returning `False`.

Before:

```python
@dataclass(frozen=True)
class Lines(CachedStep[Summary]):
    def save(self, result: Summary, cache_dir: Path) -> None:
        path = self.output_path(cache_dir)
        write_lines(path, result)

    def load(self, cache_dir: Path) -> Summary:
        return read_lines(self.output_path(cache_dir))

    def validate(self, path: Path) -> bool:
        return path.exists() and path.stat().st_size > 0
```

After:

```python
@dataclass(frozen=True)
class Lines(Step[Summary]):
    def save_result(self, result: Summary, path: Path) -> None:
        write_lines(path, result)

    def load_result(self, path: Path) -> Summary:
        return read_lines(path)  # raise if the file is broken: adag recomputes
```

`path` can be a file or a directory. See
[Caching](caching.md#results-that-are-not-one-json-document).

## Unhashable fields raise

A step field whose value adag cannot hash in a stable way raises `UnhashableValueError`. It used
to be skipped, which gave wrong cache hits. Either:

- wrap a value that does not change the result (a client, a worker count) in `Config(...)`, or
- set `strict_content_hash = False` on the step class to skip it as before.

## Dispatchers

- A custom dispatcher with only `dispatch(branch, sub, context)` still works, with a warning.
  Move to `dispatch_all(branches, context)`.
- `BatchDispatcher` is gone. Use `RemoteDispatcher` with a `JobBackend` (three methods:
  `submit`, `status`, `cancel`). [Dispatch](dispatch.md#aws-batch) has an AWS Batch recipe.

## Observer domain hooks

`NullObserver.on_embedding` and `on_model_refused` are deprecated no-ops. Declare your own typed
hook instead: see [Observers](observers.md#typed-hooks-for-your-own-values).

## Deprecated names

Each one warns with `AdagDeprecationWarning` and works until adag 1.0.

| Old | Use |
|---|---|
| `adag.Parameter` | `InputParam(name, value_type=T)` |
| `adag.StepMode` | your own enum: adag never reads it |
| `adag.BranchExecutionError` | `FanOutBranchError` |
| `CachedStep` | `Step`, with `save_result`/`load_result` for a custom format |
| `NullObserver.on_embedding`, `on_model_refused` | a typed hook of your own |
| a dispatcher with only `dispatch()` | `dispatch_all(branches, context)` |
| `adag resolve-branch --param K=V` | `--params-json` |
| a `Step` field that is also wired as an input | wire it once, as an input |

## Deprecation warnings

`adag.AdagDeprecationWarning` is a `FutureWarning`, so Python shows it in scripts and notebooks
with the default filters. Each message names the replacement.

Fail your tests on a deprecated name. In `pyproject.toml`:

```toml
[tool.pytest.ini_options]
filterwarnings = ["error::adag.AdagDeprecationWarning"]
```

Or in code:

```python
--8<-- "docs/examples/deprecation_error.py"
```

```text
adag.Parameter is deprecated and is removed in adag 1.0. Use InputParam(name, value_type=T) instead.
```

Silence them while you migrate:

```python
warnings.filterwarnings("ignore", category=adag.AdagDeprecationWarning)
```

`python -W error::adag.AdagDeprecationWarning` has no effect. Use `-W error::FutureWarning`.

## Other changes you may notice

- Python: adag needs 3.12 or newer. CI runs the tests on 3.12 and 3.13.
- A `StepResult` in a step field raises `TypeError` when you call the step. Pass it as an input.
- `cache_dir` has a new `.keys/` folder. Code that lists every file there sees it.
- `NullObserver` silences the whole run, progress logs included. Warnings and errors still log.
- `SubprocessDispatcher` runs at most one process per CPU, not one per branch. Set
  `max_concurrent=` to change it.
- `BranchPolicy.timeout_seconds` counts from when the job starts running.
