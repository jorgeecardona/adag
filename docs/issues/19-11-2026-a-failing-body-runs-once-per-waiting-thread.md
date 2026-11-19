---
title: A failing body runs once per waiting thread
status: open
area: core
owner: null
filed: 2026-11-19
needs:
  - FX-9
scope: S
verify: test
---

When a shared upstream body raises, its waiters wake and each claims the node and runs the body itself (src/adag/step.py, resolve). With 3 threads the body ran 3 times. This is documented in the resolve docstring, but it is expensive for a failing API call. Pinned by tests/test_inflight_dedup.py::test_failing_body_runs_once_per_waiter_and_every_waiter_sees_the_error.

Option: store the exception under the in-flight key for the waiters of that wave, so they raise it without running the body.