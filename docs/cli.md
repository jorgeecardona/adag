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

Add `--cache-dir DIR` to cache, and `--tree` to print the run tree.

A required `InputParam` shows as `<any>` because its type cannot be read at runtime; the value
is parsed as an int, float or bool when it looks like one, else kept as a string. Give the
param a default to fix its type.
