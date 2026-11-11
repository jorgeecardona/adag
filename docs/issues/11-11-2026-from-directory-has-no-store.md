---
title: "Pipeline.from_directory has no store= argument"
status: open
area: core
filed: 2026-11-11
scope: XS
verify: test
---

`Pipeline(..., store=)` caches in a `CacheStore`, but
[src/adag/pipeline.py](../../src/adag/pipeline.py) `from_directory` takes only `cache_dir`. A
loaded pipeline cannot read results that the original run wrote to a store.

Fix: add `store=` to `from_directory` and pass it to the new `Pipeline`. Test: save, load with the
same store, and the first run is a cache hit.
