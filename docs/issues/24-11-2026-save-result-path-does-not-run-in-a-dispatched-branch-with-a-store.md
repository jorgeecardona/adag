---
title: "A save_result step with a path cannot run in a dispatched branch when the pipeline has a store"
status: open
area: core
filed: 2026-11-24
scope: M
verify: test
---

A dispatched branch gets a `cache_dir` only when the pipeline has no store. A step that saves a
file with `save_result(result, path)` writes under `cache_dir`, so it does not run on a worker of
a pipeline that has a store. Large files, such as model weights, cannot cross machines.

Found while designing runs on cloud machines. Fix to try: let such a result go through the store
as a blob, or give the worker a scratch `cache_dir` and copy the file to the store on save.
