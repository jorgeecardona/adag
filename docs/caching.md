# Caching

Set `cache_dir` on the `Pipeline` and every step with a `BaseModel` result is cached there.
A step runs again only when its cache key changes.

## What is in the key

- The step class name, or its `kind` if it has one.
- The source code of the step class body.
- Every dataclass field, except `Config` fields.
- The cache keys of upstream steps, `InputParam` values, and `FileSource` file content.

So a change to any of these recomputes that step and everything downstream. Nothing else
recomputes.

## Keep a field out of the key: Config

Wrap execution-only settings (worker counts, clients, timeouts) in `Config`. `run()` receives
the plain value.

```python
--8<-- "docs/examples/caching.py"
```

```text
Scale(factor=2, workers=Config(4))            ran: True
Scale(factor=2, workers=Config(4))            ran: False
Scale(factor=2, workers=Config(16))           ran: False
Scale(factor=3, workers=Config(4))            ran: True
```

(output trimmed: log lines removed)

## Supported field types

Fields are hashed by `adag.hashing.encode_value`: `None`, `bool`, `int`, `float`, `str`,
`bytes`, `Enum`, `Path`, `UUID`, `type`, `BaseModel`, and `list`, `tuple`, `dict`, `set` of
those. A field of another type raises `UnhashableValueError`: wrap it in `Config`, or set
`strict_content_hash = False` on the class to skip such fields.

A `Path` field is hashed by its text. To hash a file's content, pass a `FileSource`.

## Limits

- Only the step's own class body is hashed. An edit to a base class or to a helper function
  the step calls does not change the key. Change a field or the `kind` version when it should.
- Any edit inside the class body changes the key, comments included.
- A step whose result is not a `BaseModel` is not cached.
- Keys are 12 hex characters (48 bits) of SHA-256.

## On disk

Each result is `<cache_dir>/<key>` (JSON) plus `<key>.done`, written after the result. A
result without its `.done` file, an empty file, or a file that does not parse is a miss.
