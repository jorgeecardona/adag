# Inputs

Feed values into a graph with `InputParam` (a value given at `execute()`) or a source (a file or
folder whose content is part of the cache key).

- `InputParam[T]("name")`: required; pass `name=...` to `execute()`.
- `InputParam[T].with_default("name", value)`: optional.
- `FileSource(path)`: the file's text. Its content is hashed, so editing the file recomputes.
- `LinesSource(path)`, `JSONLSource(path)`: the file as lines, or as parsed JSON lines.
- `DirSource(path, pattern)`: a folder; adding, removing or editing a matching file recomputes.

```python
--8<-- "docs/examples/inputs.py"
```

```text
words=5
words=3
words=8
```
