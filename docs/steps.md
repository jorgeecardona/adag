# Steps and pipelines

Write each unit of work as a step, wire steps by calling them, and run the graph with a
`Pipeline`.

## Write a step

A step is a frozen dataclass that subclasses `Step[Result]`, where `Result` is a pydantic
`BaseModel`. Its `run()` method takes the step's inputs by name and returns a `Result`.

- Dataclass fields are configuration. They are part of the cache key.
- `run()` arguments are inputs. They come from other steps, `InputParam`s, or sources.
- A field with the same name as a `run()` argument fills that argument.

## Wire and run

Calling a step instance returns a `StepResult`: a node in the graph, not a value. Pass it to
another step's call to make an edge. `Pipeline(output).execute(**params)` resolves the graph.

```python
--8<-- "docs/examples/quickstart.py"
```

```text
computing square
value=17
value=17
```

(output trimmed: log lines removed)

## Errors before running

Two errors and one warning can stop a graph before any step runs:

- `DeprecationWarning`, when you call the step: a field holds a `Step` and the same name is also
  wired as an input. Wire an upstream step once, as an input. This becomes a `TypeError` in 0.4.
- `CyclicPipelineError`, when you build the `Pipeline`: the graph has a cycle.
- `UnboundInputError`, when you call `pipeline.validate()`: a `run()` argument is neither wired,
  a field, nor has a default. Without `validate()`, `execute()` fails with a `TypeError` from
  `run()`.

## Reserved names

`observer`, `executor`, `dispatcher`, `parallel` and `max_workers` are keyword arguments of
`execute()`, and `async_executor` of `aexecute()`. Do not use them as `InputParam` names.
