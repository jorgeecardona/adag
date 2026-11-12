# Save and load pipelines

`Pipeline.save(folder)` writes the graph as JSON. `Pipeline.from_directory(folder,
step_registry)` rebuilds it with the same cache keys, so another process finds the same
cached results.

Steps that can be saved subclass `SerializableStep`. What a subclass implements:

1. `kind`: a class attribute such as `"greet/v1"`. The `step_registry` maps it back to the class.
   A subclass without it raises `TypeError` when the class is defined.
2. `spec`: an optional field holding a pydantic model with the step's configuration.
3. `from_directory(cls, path)`: needed only when the step has a required field other than `spec`.
   The default rebuilds `cls(spec=...)`, or `cls()` without a spec. Write your own with
   `cls(spec=cls.load_spec(path, Spec))` plus whatever state you saved.
4. `save_artifacts(self, path)`: override only to save extra state (weights, tokenizer files) next
   to `step.json`. Call `super().save_artifacts(path)` first.

A step that the default cannot rebuild, and that has no `from_directory`, raises `TypeError` in
`save()`, before `pipeline.json` is written. You find out when you save, not months later when you
load.

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

`pipeline.metadata` is typed `adag.JsonValue`, not `Any`. A type checker accepts any JSON-shaped
value going in, and asks you to narrow it (for example `isinstance(meta, dict)`) before you index
it.
