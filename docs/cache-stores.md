# Cache stores

`ItemCache` (the per-item cache behind `BatchStep`) can store items in other backends. Step
results are always files under `cache_dir`.

| Store | Module | Extra |
|---|---|---|
| `FileSystemCacheStore` (default) | `adag.cache_store` | none |
| `MemoryCacheStore` | `adag.cache_store` | none |
| `SqliteCacheStore` | `adag.cache_store` | none |
| `SqlCacheStore` | `adag.cache_store` | `adag[sql]` |
| `RedisCacheStore` | `adag.cache_store` | `adag[redis]` |

```python
--8<-- "docs/examples/item_cache_store.py"
```

```text
cached: 1 missing: 2
text='first'
```

(output trimmed: log lines removed)
