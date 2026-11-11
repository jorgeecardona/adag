---
title: "BatchStep examples wire the upstream step twice: as a field and as an input"
status: done
area: core
filed: 2026-11-11
scope: S
verify: inspect
---

[tests/test_batch.py](../../tests/test_batch.py) builds `DoubleBatch(source=source)(source=source(...))`:
the step instance is a dataclass field (hashed as a sub-step) and its result is also the run
input. Decide one way, document it, and simplify the tests. The batch doc page uses an
`InputParam` and avoids the pattern.
