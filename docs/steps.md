# Steps and pipelines

Write each unit of work as a step, wire steps by calling them, and run the graph with a
`Pipeline`.

## Write a step

A step is a frozen dataclass that subclasses `Step[Result]`, where `Result` is usually a pydantic
`BaseModel` (see [Caching](caching.md#results-that-are-not-one-json-document) for other types).
Its `run()` method takes the step's inputs by name and returns a `Result`.

- Dataclass fields are configuration. They are part of the cache key.
- `run()` arguments are inputs. They come from other steps, `InputParam`s, or sources.
- A field reaches `run()` only if `run()` names it. Otherwise read it from `self`, for example
  `self.level`. The field is part of the cache key either way.
- A `Config` field in `self` is the `Config` wrapper: read `self.concurrency.value`. A parameter
  named `concurrency` gets the plain value.
- A `run()` with `**kwargs` gets every field. A name that `run()` does not accept, and that is
  not a field, is a `TypeError`.
- A wired input with the same name as a field wins.
- Name the parameters you need. pyright strict accepts that override without an ignore comment.
- Define `run()` on the step class itself. A `run()` that comes from a mixin that is not a `Step`
  still gets every field, so give it `**kwargs`.

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
- `UnacceptedInputError`: a wired input is not a parameter of `run()` and `run()` has no
  `**kwargs`. Without `validate()`, `execute()` fails with a `TypeError`.

`validate()` checks `arun()` and the batch methods (`items_data`, `process_item`, `assemble`) the
same way.

## Reserved names

`observer`, `executor`, `dispatcher`, `parallel` and `max_workers` are keyword arguments of
`execute()`. `observer`, `async_executor` and `max_concurrency` are keyword arguments of
`aexecute()`. Do not use them as `InputParam` names.
