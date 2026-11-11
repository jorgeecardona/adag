---
title: "Step.produces and Step.accepts are declared but nothing uses them"
status: done
fixed_in: fd066b0
area: core
filed: 2026-11-11
scope: XS
verify: inspect
---

The class attributes in [src/adag/step.py](../../src/adag/step.py) are introspection hooks from
the internal origin. `Pipeline` never checks them. Remove them before 1.0, or make
`Pipeline.validate()` enforce them.
