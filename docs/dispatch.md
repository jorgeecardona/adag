# Dispatch

A dispatcher decides where fan-out branches run. `SubprocessDispatcher` runs each branch in a
new Python process. Workers write results to `cache_dir`, and the main process reads them by
cache key.

The pipeline must live in a file, and the `FanOutStep` names that file and its variable, so a
worker can import it and rebuild the branch:

```python
--8<-- "docs/examples/dispatch_pipeline.py"
```

```python
--8<-- "docs/examples/dispatch.py"
```

```text
{'a': 2, 'b': 4, 'c': 6}
ran in other processes: True
```

(output trimmed: log lines removed)

- `cache_dir` is required.
- Params reach workers as JSON, so they must be JSON values.
- `BranchPolicy(max_retries=, timeout_seconds=)` sets retries and a timeout per branch.
