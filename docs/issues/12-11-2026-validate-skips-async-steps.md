---
title: "Pipeline.validate() checks nothing for an AsyncStep"
status: open
area: core
filed: 2026-11-12
scope: S
verify: test
---

`Pipeline.validate()` inspects the signature of `run()`. An `AsyncStep` takes its inputs in
`arun()`, so `validate()` finds no unbound inputs for it and passes. The missing input then fails
when the step resolves.

Fix: read the signature of `arun()` for an `AsyncStep` in `_check_inputs_bound` in
[src/adag/pipeline.py](../../src/adag/pipeline.py). Test: an `AsyncStep` with an unwired required
input raises `UnboundInputError` from `validate()`.
