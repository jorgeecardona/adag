---
title: "No early cutoff: a downstream key follows the upstream key, not the upstream result"
status: open
area: core
filed: 2026-11-22
scope: L
verify: test
---

Found while trying adag for the radiant Lean explainer build (one `Step` per Lean declaration,
each wired to the steps of the declarations it cites). The key of a step includes the key of each
upstream node (`upstream` rows in `explain_key`). So when an upstream step recomputes, every step
below it recomputes, even if the upstream result is byte-identical.

Probe (adag 0.3.39, no model, runs in a second): `A = Gen(source=...)()`, `B = Gen(...)(dep0=A)`.
Change only the part of `A`'s source that `A.run` ignores (the proof text; `run` reads only the
signature). `A`'s key changes, as it should. `B`'s key changes too. `B` recomputes with the same
input value it had before.

For an LLM or other costly step this is the expensive case: one edit to a lemma's proof
regenerates and re-judges every declaration that cites it, when the result of the lemma (its
plain-language statement) did not change. Build systems call the fix early cutoff (Shake, Bazel
with content-addressed outputs): after an upstream step finishes, key its dependents by the hash of
its result, so an unchanged result stops the cascade.

Why it is not a small change: today a key is known before any run, which is what `explain_key`,
`dag()` and the key records rely on. Early cutoff needs the key of a dependent to be computed
after its upstream result exists. Options to weigh:

- An opt-in per step, `key_by_result = True`: the key of this step uses the result hash of its
  upstream nodes, not their keys. `explain_key` shows `upstream_result` rows and says "known after
  the upstream step ran", as it does for an `expansion`.
- Leave the default as it is: a result hash needs the upstream result to be cached or computed
  first, and a cold run computes everything anyway.

Done when: a test builds the probe above, changes only the ignored part of `A`'s source, and
asserts that `B` is a cache hit when it is opted in, and still recomputes by default.
