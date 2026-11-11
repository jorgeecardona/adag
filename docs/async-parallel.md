# Async and parallel execution

Use `AsyncStep` when a step awaits I/O, and `parallel=True` when independent steps block.

- `AsyncStep`: implement `async def arun(...)` and run the graph with
  `await pipeline.aexecute()`. Sync steps run unchanged inside `aexecute()`.
- `FanOutStep` under `aexecute()`: runs in a worker thread, with its branches in-process.
  `aexecute()` has no `dispatcher=`. A dispatcher passed there is ignored without an error.
- `StepResult.aresolve(context)`: resolve one node yourself. Use it when you schedule the graph
  (for example one `asyncio.gather` per level of `StepResult` nodes you sorted). Pass one
  `ResolveContext` to every call: it memoizes each node, so nothing runs twice.
- `execute(parallel=True, max_workers=N)`: run independent steps on a thread pool.
- `timeout_s` (class attribute): a step that runs longer raises `StepTimeout`.

```python
--8<-- "docs/examples/async_parallel.py"
```

```text
aexecute: n=3
parallel: n=3, both Slow steps at once: True
```
