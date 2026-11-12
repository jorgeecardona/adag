---
title: "A missing FileSource wired as an input does not block the key"
status: open
area: core
filed: 2026-11-12
scope: S
verify: test
---

A `FileSource` whose file is missing blocks the cache key when it is a step field: `key` is
`None` and `explain_key` names the field. Wired as an input, the same missing file still gets a
key, and the run fails later with `FileNotFoundError`.

The `Deferred` branch of `StepResult._key_chunks` in [src/adag/step.py](../../src/adag/step.py)
encodes the `None` file hash into bytes and never yields `None`. The module docstring of
[src/adag/io.py](../../src/adag/io.py) says a missing file blocks downstream steps, and
[docs/inputs.md](../inputs.md) shows only the input form, so the common case differs from the
documented one.

Fix: decide which behavior is right. If a missing file should block, make the input path return
`None` and show `BLOCKED` in `dag()`. If not, correct the `io.py` docstring and
[docs/cache-keys.md](../cache-keys.md). Test: one graph with the file as a field, one as an input,
and the same `key` and state for both.
