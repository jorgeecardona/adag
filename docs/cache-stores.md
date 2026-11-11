# Cache stores

Pass `store=` to `Pipeline` to keep step results and `BatchStep` items in a `CacheStore`
instead of files under `cache_dir`.

| Store | Module | Extra |
|---|---|---|
| `FileSystemCacheStore` (default) | `adag.cache_store` | none |
| `MemoryCacheStore` | `adag.cache_store` | none |
| `SqliteCacheStore` | `adag.cache_store` | none |
| `SqlCacheStore` | `adag.cache_store` | `adag[sql]` |
| `RedisCacheStore` | `adag.cache_store` | `adag[redis]` |

```python
--8<-- "docs/examples/pipeline_store.py"
```

```text
greeting ana
greeting bo
greeting cy
7 keys in cache.db
```

The second run greets only `cy`: `ana` and `bo` are read from the store.

## Open a store from a URL

`store_from_url(url)` opens a store from text, so a remote worker can open the same store as
the main process (see [Dispatch](dispatch.md)).

- `sqlite:///relative.db` or `sqlite:////absolute.db`: `SqliteCacheStore`.
- `redis://host:6379/0`: `RedisCacheStore`.
- `postgresql://...` or any other SQLAlchemy URL: `SqlCacheStore`.
- `file:///path` or a plain path: `FileSystemCacheStore`.

## Use one ItemCache directly

`ItemCache` also takes `store=` when you use it outside a pipeline:

```python
--8<-- "docs/examples/item_cache_store.py"
```

```text
cached: 1 missing: 2
text='first'
```
