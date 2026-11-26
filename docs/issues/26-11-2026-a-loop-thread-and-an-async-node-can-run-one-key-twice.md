---
title: "A step body can run twice when a thread and the event loop reach one key"
status: open
area: core
filed: 2026-11-26
scope: M
verify: test
---

`ResolveContext` keeps two maps of the nodes being resolved: `_inflight` for the sync path and
`_ainflight` for the async path. A `LoopStep` resolves its rounds on the sync path in a thread.
When a round and an async node have the same cache key at the same time, both run the body and
both write the entry. Reproduced on 0.3.44: one key, two runs.

Proposal: one map that both paths claim.

Trap, found with a musil model: a thread that waits for the claim of a loop task must not hold a
slot of the loop's default pool, because the task's body may need that slot. With a pool of one
slot the model deadlocks.

Done when: a test shows one run, and a second test with a default pool of one worker finishes.
