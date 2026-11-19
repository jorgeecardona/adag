---
title: "FanOutStep key ignores an InputParam used inside a branch"
status: open
area: core
filed: 2026-11-19
scope: S
verify: test
---

`FanOutStep.content_hash()` takes no `params`. A branch built by the `factory` (or the
`template`) can use an `InputParam`, but its value does not reach the key of the fan-out node.
Change only that param and the node key stays the same, so a downstream cached step can serve a
result that was computed with the old value.

The static form (`factory=`) already behaves this way. A template fan-out copies it. The gap is
the node key and what is downstream of it.

Fix: give the fan-out node's key the params of the run, for example by hashing each branch with
`params=` in `FanOutStep._template_hash` and the static path in
[src/adag/fan_out.py](../../src/adag/fan_out.py), and have `StepResult` pass them. Test: a
branch that reads an `InputParam`, a cached step downstream, change the param, and expect the
downstream step to run again.
