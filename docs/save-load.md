# Save and load pipelines

`Pipeline.save(folder)` writes the graph as JSON. `Pipeline.from_directory(folder,
step_registry)` rebuilds it with the same cache keys, so another process finds the same
cached results.

Steps that can be saved subclass `SerializableStep`, set a `kind` string, keep their
configuration in a `spec` model, and implement `from_directory`.

```python
--8<-- "docs/examples/save_load.py"
```

```text
['pipeline.json', 'step.json']
text='Hello, Ada'
same key: True
```

(output trimmed: log lines removed)

The `kind` string and the `step.json` layout (`kind`, `spec`) are the saved format. Change the
version in `kind` (`/v1` to `/v2`) when a step's spec changes shape.
