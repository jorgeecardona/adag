# Fan-out

`FanOutStep` builds one sub-graph per key of a dict and returns a dict of results with the
same keys. Each branch is cached like any other step.

- `over`: the dict to iterate.
- `factory(key, value)`: returns the branch's `StepResult`.
- `max_workers`: run branches on a thread pool of this size. The default runs them one by one.

```python
--8<-- "docs/examples/fan_out.py"
```

```text
{'intro': 6, 'body': 19, 'end': 4}
```

A failing branch raises `FanOutBranchError`, which names the key. To run branches in separate
processes, see [Dispatch](dispatch.md).
