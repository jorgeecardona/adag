# Command line

`adag run` runs a target from a pipeline file. Every module-level `StepResult` in the file is
a target.

```python
--8<-- "docs/examples/cli_pipeline.py"
```

1. List the targets:

    ```bash
    adag run cli_pipeline.py -h
    ```

2. List a target's params:

    ```bash
    adag run cli_pipeline.py --target total -h
    ```

3. Run it:

    ```bash
    adag run cli_pipeline.py --target total --param x=4
    ```

```text
total: Number(value=17)
```

(output trimmed: log lines removed)

Add `--cache-dir DIR` to cache, and `--tree` to print the run tree.

Give a required `InputParam` its type with `value_type=`, for example
`InputParam[int]("x", value_type=int)`. The `[int]` alone is lost at runtime. Without
`value_type` or a default, the param shows as `<any>` and the value is parsed as an int, float or
bool when it looks like one, else kept as a string.

## Worker command

`adag resolve-branch` runs one fan-out branch and writes its result to the cache. A dispatcher
builds this command (`BranchJob.command()`); you rarely type it.

```bash
adag resolve-branch pkg.pipeline --fan-out per_key --key a --cache-dir /shared/cache
```

- `pkg.pipeline`: a module name or a file path.
- `--cache-dir DIR` or `--store-url URL`: one is required.
- `--params-json JSON`: the run params as one JSON object.
- `--expect-hash HASH`: exit 2 when the worker derives a different branch hash.

See [Dispatch](dispatch.md).
