---
title: "A CacheStore cannot be given to a Pipeline or a BatchStep"
status: done
fixed_in: 6e5ec6e
area: core
filed: 2026-11-11
scope: M
verify: test
---

`ItemCache` takes `store=`, but `BatchStep` builds its `ItemCache` from `cache_dir` only, and
step results are always files. So the SQLite, SQL and Redis stores are usable only by calling
`ItemCache` directly (see [docs/cache-stores.md](../cache-stores.md)).

Fix: `Pipeline(..., store=CacheStore)` used for both step results and item caches. Test: the
quickstart and the batch example pass against every store.
