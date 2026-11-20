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
- A file that does not exist: the run fails with `FileNotFoundError` when the source is read.
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
