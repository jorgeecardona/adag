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

## Caller metadata

Pass `metadata=` to keep your own data with a saved pipeline: what it is for, which dataset it
was fitted on, a schema version. It can be any JSON value.

```python
pipeline = Pipeline(output, metadata={"fitted_on": "2026-11", "schema": 2})
pipeline.save(folder)
Pipeline.from_directory(folder, step_registry).metadata  # {"fitted_on": "2026-11", "schema": 2}
```

`save()` writes it to `pipeline.json` under `"metadata"`. adag does not read it for anything else.
A value that is not JSON raises `TypeError` when the `Pipeline` is built.
