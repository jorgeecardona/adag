---
title: "execute(x=None) runs with the default of x but hashes None"
status: open
area: core
filed: 2026-11-12
scope: S
verify: test
---

For an `InputParam` with a default, `execute(x=None)` runs the step with the default:
`InputParam.resolve` in [src/adag/deferred.py](../../src/adag/deferred.py) treats a `None` value
as missing. The cache key still encodes `None`, so `explain_key` shows `None` for a step that ran
with the default, and the same result is saved under two keys.

Fix: make the key use the value the step receives. Changing it changes the key of any step that
was run with an explicit `None`, so it needs a `KEY_FORMAT` note in the CHANGELOG. Test: `x=None`
and no `x` give the same key and the same result.
