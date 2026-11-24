---
title: "No CacheStore for S3 or Google Cloud Storage"
status: open
area: core
filed: 2026-11-24
scope: M
verify: test
---

Only `SqlCacheStore` and `RedisCacheStore` work across machines. A user who starts jobs on Modal,
vast.ai or Google Cloud needs a database that both the laptop and the cloud machine can reach.
An object store is cheaper and already there.

Add `S3CacheStore` and `GcsCacheStore` as extras (`adag[aws]`, `adag[gcp]`), with the same
conformance tests as the other stores. Keep the key records (`.keys/<key>.json`) working.
