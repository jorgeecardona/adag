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
`on_step_finished`, `on_fan_out_branch`, `on_fan_out_branch_failed`, and `on_event` for
progress events (`step.saved`, `batch.*`, `item_cache.*`, `branch.*`).

- `NullObserver` drops all of them, so a run prints nothing.
- Warnings and errors (a timed-out branch, a cache invariant violation) are always logged
  with structlog, so an observer cannot hide a problem.
- Inside a step body, send your own event with `adag.deferred.emit("my.event", n=3)`.
