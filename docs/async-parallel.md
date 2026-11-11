# Async and parallel execution

Use `AsyncStep` when a step awaits I/O, and `parallel=True` when independent steps block.

- `AsyncStep`: implement `async def arun(...)` and run the graph with
  `await pipeline.aexecute()`. Sync steps run unchanged inside `aexecute()`.
- `execute(parallel=True, max_workers=N)`: run independent steps on a thread pool.
- `timeout_s` (class attribute): a step that runs longer raises `StepTimeout`.

```python
--8<-- "docs/examples/async_parallel.py"
```

```text
aexecute: n=3
parallel: n=3, both Slow steps at once: True
```
