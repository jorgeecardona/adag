# Inputs

Feed values into a graph with `InputParam` (a value given at `execute()`) or a source (a file or
folder whose content is part of the cache key).

- `InputParam[T]("name")`: required; pass `name=...` to `execute()`. Add `value_type=T` so the
  CLI parses `--param` as `T`.
- `InputParam[T].with_default("name", value)`: optional.
- `FileSource(path)`: the file's text. Its content is hashed, so editing the file recomputes.
- `LinesSource(path)`, `JSONLSource(path, RecordType)`: the file as lines, or as JSON lines parsed
  into `RecordType` (a Pydantic model; for untyped lines use `LinesSource` and `json.loads`).
- A path is a `str` or a `pathlib.Path`. The source stores it as a `str`.
- `DirSource(path, pattern)`: a folder; adding, removing or editing a matching file recomputes.
- A `FileSource`, `LinesSource` or `JSONLSource` on a directory raises `IsADirectoryError`. The
  message names the source and the path. Use `DirSource` for a folder.
- A file or folder that does not exist: the steps that read it have no cache key, and `dag()`
  draws them and the steps downstream `BLOCKED`. A missing file also fails the run with
  `FileNotFoundError` when the source is read.
- `DirSource` uses `pattern` as a glob. The default `"*.jsonl"` is not recursive: pass
  `"**/*.jsonl"` for nested folders.

```python
--8<-- "docs/examples/inputs.py"
```

```text
words=5
words=3
words=8
```

## Your own input

Subclass `Deferred` to feed a graph from somewhere else:

1. Define `resolve(context)`. It returns the value.
2. Define `content_hash()`. It returns a text that changes when the value changes.

```python
--8<-- "docs/examples/custom_input.py"
```

```text
greeting ana
hello ana
hello ana
greeting bo
hello bo
```

Without `content_hash`, or when it returns `None`, the steps that read the input have no cache
key. They and the steps downstream run every time.
