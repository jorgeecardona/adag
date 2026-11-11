---
title: "RemoteDispatcher store_url on a cache_dir pipeline loses branch results"
status: open
area: core
filed: 2026-11-11
scope: S
verify: test
---

`RemoteDispatcher(store_url=...)` on a pipeline with only `cache_dir` sends both `--cache-dir`
and `--store-url`. The worker writes to the store, the main process reads `cache_dir`, and the
branch fails with "job succeeded but no result is in the cache". Found by reading
[src/adag/dispatch.py](../../src/adag/dispatch.py); not run.

Fix: raise `ValueError` in `dispatch_all` when `store_url` is set and the pipeline has no store.
Test: that combination raises before any job is submitted.
