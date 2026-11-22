---
title: "An edit to a module-level constant that run() reads does not change the key"
status: open
area: docs
filed: 2026-11-22
scope: XS
verify: test
---

Found with the same probe as the early-cutoff issue. The `code` row of a key is "a digest of the
step class source" ([cache keys](../cache-keys.md)). A step whose `run()` reads a module-level
constant, for example an LLM prompt `PROMPT = "..."`, keeps the same key when the constant is
edited: probe, `PROMPT = "v1"` then `"v2"`, `explain_keys()` is identical, and a cached result
from the old prompt is served.

The workaround is to make the value a field (fields are in the key). That is a trap because the
code looks like it is covered: the docs say code is in the key and do not say what code means.

Do one of:

- Document it where `code` is described: only the class source is hashed, not module globals,
  imports or called functions; put anything that changes the result in a field.
- Or add an opt-in `key_extra: tuple[object, ...]` class attribute (or method) whose values are
  hashed into the key, so a module constant can be listed without becoming a field.

Done when: the docs say it in `cache-keys.md` and the step docs, and (if `key_extra` is added) a
test shows that editing the listed constant changes the key.
