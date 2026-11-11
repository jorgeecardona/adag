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

(output trimmed: log lines removed)

The hooks are the methods of `PipelineObserver`: `on_step_start`, `on_step_cached`,
`on_step_finished`, `on_fan_out_branch`, `on_fan_out_branch_failed`. Inside a step body,
`adag.deferred.current_observer()` returns the run's observer.

Some messages (`Cached to disk`, `batch.*`, `item_cache.*`) are logged with structlog
directly, not through the observer. Configure structlog to filter them.
