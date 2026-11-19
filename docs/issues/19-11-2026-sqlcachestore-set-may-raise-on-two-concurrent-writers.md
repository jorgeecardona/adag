---
title: SqlCacheStore.set may raise on two concurrent writers
status: open
area: core
owner: null
filed: 2026-11-19
needs:
  - FX-10
scope: S
verify: probe
---

SqlCacheStore.set deletes then inserts on a primary key (src/adag/cache_store.py). Two concurrent writers on PostgreSQL at READ COMMITTED may both pass the delete and the second insert then raises a unique violation. Unverified: read from the code, not run on a real PostgreSQL.

Check on a real PostgreSQL. Fix: INSERT ... ON CONFLICT DO UPDATE, or catch the violation and treat it as success, since a key's value is fixed by its content.