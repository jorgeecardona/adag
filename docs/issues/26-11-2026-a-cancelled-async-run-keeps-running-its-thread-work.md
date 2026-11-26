---
title: "A cancelled async run keeps running the work that is in a thread"
status: open
area: core
filed: 2026-11-26
scope: M
verify: test
---

Cancelling an `aresolve`, `aresolve_all` or `aexecute` task does not stop work that runs in a
thread. The thread goes on to the end.

Measured on 0.3.44:

- A `LoopStep` cancelled after 2 rounds had run 10 rounds 2.5 s later.
- A context with a dispatcher, cancelled after 1 branch, had sent 8 branches 2.5 s later (a fake
  dispatcher). On a real backend, jobs would start after the cancel and cost money.
- A `BatchStep` runs its remaining items.

Proposal: a cancel flag on the `ResolveContext` that the sync `resolve`, the `LoopStep` rounds,
the `BatchStep` item loop and the dispatcher loop check. The dispatcher cancels its live jobs.

Done when: a test cancels each of the three and no further round, item or job starts.
