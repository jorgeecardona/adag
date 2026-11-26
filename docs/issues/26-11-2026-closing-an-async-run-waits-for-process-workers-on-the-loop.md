---
title: "Cancelling aexecute with a process step blocks the event loop until the step ends"
status: open
area: core
filed: 2026-11-26
scope: S
verify: test
---

`Pipeline.aexecute` calls `context.close()` in its cleanup, and `close` runs
`ProcessPoolExecutor.shutdown(wait=True)` on the event loop. After a cancel, the worker process
is still running its step, so the loop waits for it. Measured on 0.3.44: a stall of 2.4 s for
a 3 s step.

Proposal: run the close in a thread, or do not wait for the workers after a cancel.

Done when: a test shows that another task runs while `aexecute` is cancelled.
