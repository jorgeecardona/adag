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

`Pipeline` checks the graph when you build it:

- `UnboundInputError`: a `run()` argument is neither wired, a field, nor has a default.
- `TypeError` when you call the step: a field holds a `Step` and the same name is also wired as
  an input. Wire an upstream step once, as an input. Do not also keep it as a field.
- `CyclicPipelineError`: the graph has a cycle.

## Reserved names

`observer`, `executor`, `dispatcher`, `parallel` and `max_workers` are keyword arguments of
`execute()`. Do not use them as `InputParam` names.
