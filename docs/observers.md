# Observers

An observer receives hooks during a run: step start, cache hit, step finished, fan-out branch.
Pass one with `execute(observer=...)`.

- `LoggingObserver`: the default. Logs each step with structlog.
- `NullObserver`: silent. Subclass it and override only the hooks you need.
- `RunTreeObserver`: collects a timing tree; print it with `render_tree()`.

```python
--8<-- "docs/examples/observers.py"
```

```text
first run:
Square  ● 0ms
└─ Load  ● 0ms
second run:
Square  ⟳ cached
```

The hooks are the methods of `PipelineObserver`: `on_step_start`, `on_step_cached`,
`on_step_finished`, `on_fan_out_branch`, `on_fan_out_branch_failed`, `on_loop_round`, and
`on_event` for progress events (`step.saved`, `batch.*`, `item_cache.*`, `branch.*`).

- `NullObserver` drops all of them, so a run prints nothing.
- Warnings and errors (a timed-out branch, a cache invariant violation) are always logged
  with structlog, so an observer cannot hide a problem.
- Inside a step body, send your own event with `adag.deferred.emit("my.event", n=3)`.

## Typed hooks for your own values

Use a typed hook when your code consumes a value from a run (a score, a vector, a model name),
instead of logging it. adag has no domain hooks of its own. Declare one yourself:

1. A `runtime_checkable` `Protocol` with your method, here `on_score(score: float)`.
2. One function that calls the method when the observer has it, and does nothing otherwise.
3. An observer that implements the method. It subclasses `NullObserver` for the other hooks.

Call the function from either place:

- Inside a step, with `context.observer` (override `execute`, call `super().execute`).
- From any helper a step calls, with `current_observer()`.

```python
--8<-- "docs/examples/typed_observer_hook.py"
```

```text
[0.7, 1.4]
```

(output trimmed: log lines removed)

A strict type checker sees `float` at both ends. A plain `NullObserver` or `LoggingObserver`
keeps working, because the function skips an observer without the method.

`on_event(event, fields)` stays untyped (`dict[str, Any]`): it carries progress events, not
values a caller consumes.

The deprecated `NullObserver.on_embedding` and `on_model_refused` do nothing. Replace each with a
typed hook as above.
