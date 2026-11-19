# Steps and pipelines

Write each unit of work as a step, wire steps by calling them, and run the graph with a
`Pipeline`.

## Write a step

A step is a frozen dataclass that subclasses `Step[Result]`, where `Result` is usually a pydantic
`BaseModel` (see [Caching](caching.md#results-that-are-not-one-json-document) for other types).
Its `run()` method takes the step's inputs by name and returns a `Result`.

- Dataclass fields are configuration. They are part of the cache key.
- `run()` arguments are inputs. They come from other steps, `InputParam`s, or sources.
- adag passes every field to `run()` by name. `run()` must accept each field, or take `**kwargs`.
  A wired input with the same name as a field wins.

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

adag checks the graph at three moments, before any step runs.

When you call a step:

- `TypeError`: a field holds a `StepResult`. A field is part of the key, not an edge. Pass the
  node as a call input: `MyStep()(name=node)`.
- `TypeError`: an input's type does not match the type hint of that `run()` argument.
- `AdagDeprecationWarning`: a field holds a `Step` and the same name is also wired as an input.
  The run continues. Wire the upstream step once, as an input. This is a `TypeError` in adag 1.0.

When you build the `Pipeline`:

- `CyclicPipelineError`: the graph has a cycle.

When you call `pipeline.validate()`:

- `UnboundInputError`: a `run()` argument is not wired, is not a field, and has no default.
  Without `validate()`, `execute()` fails with a `TypeError` from `run()`.

## Reserved names

`observer`, `executor`, `dispatcher`, `parallel` and `max_workers` are keyword arguments of
`execute()`. `observer`, `async_executor` and `max_concurrency` are keyword arguments of
`aexecute()`. Do not use them as `InputParam` names.
