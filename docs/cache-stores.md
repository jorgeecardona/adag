# Cache stores

Pass `store=` to `Pipeline` to keep step results and `BatchStep` items in a `CacheStore`
instead of files under `cache_dir`.

All stores import from `adag.cache_store`.

| Store | Extra | `runs_on` |
|---|---|---|
| `FileSystemCacheStore` (default) | none | `"loop"` |
| `MemoryCacheStore` | none | `"loop"` |
| `SqliteCacheStore` | none | `"loop"` |
| `SqlCacheStore` | `adag[sql]` | `"loop"` for SQLite, else `"thread"` |
| `RedisCacheStore` | `adag[redis]` | `"thread"` |

```python
--8<-- "docs/examples/pipeline_store.py"
```

```text
greeting ana
greeting bo
greeting cy
9 keys in cache.db
```

The second run greets only `cy`: `ana` and `bo` are read from the store.

The 9 keys: 3 items (`greet/<key>.json`), and for each of the 2 runs a result `<key>`, its
`<key>.done` marker, and its key record `.keys/<key>.json` (see [Caching](caching.md)).

## Keep a network store off the event loop

`runs_on` says where a store is called under `aexecute`, `aresolve` and `aresolve_all`.

- `"thread"`: in a thread of adag's own pool. Other tasks keep running while the store answers.
- `"loop"`: on the event loop. The loop waits for each call.

Set it on your own store to move its calls to a thread:

```python
class S3CacheStore:
    runs_on = "thread"

    def get(self, key: str) -> str | None: ...
```

Rules for a `"thread"` store:

1. Its five methods must be safe to call from several threads at once.
2. Set a timeout on its client. A call that never returns holds a thread, and its node waits.
3. A cancel during a write lets the write end. Until then the node stays claimed. If the event
   loop stops first, the claim is dropped and a failed write may not be logged.

With your own `asyncio.gather` over `aresolve` and a `"thread"` store, `RunTreeObserver` can show
a node under another task's node. `aresolve_all` and `aexecute` do not.

Turn it off with `store.runs_on = "loop"`. A store without the attribute runs on the loop.
`execute()` and `resolve()` ignore it.

## Open a store from a URL

`store_from_url(url)` opens a store from text, so a remote worker can open the same store as
the main process (see [Dispatch](dispatch.md)).

- `sqlite:///relative.db` or `sqlite:////absolute.db`: `SqliteCacheStore`.
- `redis://host:6379/0` or `rediss://host:6379/0` (TLS): `RedisCacheStore`.
- `postgresql://...` or any other SQLAlchemy URL: `SqlCacheStore`.
- `file:///path` or a plain path: `FileSystemCacheStore`.
- `memory:`: raises `ValueError`; a memory store cannot be shared with a worker.

## Use one ItemCache directly

`ItemCache` also takes `store=` when you use it outside a pipeline:

```python
--8<-- "docs/examples/item_cache_store.py"
```

```text
cached: 1 missing: 2
text='first'
```
