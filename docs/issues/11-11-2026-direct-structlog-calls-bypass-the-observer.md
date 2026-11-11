---
title: "Some log lines bypass the observer, so NullObserver does not silence a run"
status: open
area: core
filed: 2026-11-11
scope: S
verify: test
---

`Cached to disk` ([src/adag/step.py](../../src/adag/step.py)), `batch.*`
([src/adag/batch.py](../../src/adag/batch.py)), `item_cache.*`, `branch.*` and `dag.svg.saved` call
structlog directly. A run with `execute(observer=NullObserver())` still prints them.

Fix: route them through observer hooks (for example `on_step_saved`, `on_batch_progress`) so the
observer decides. Test: a run with `NullObserver` logs nothing (`structlog.testing.capture_logs`).
