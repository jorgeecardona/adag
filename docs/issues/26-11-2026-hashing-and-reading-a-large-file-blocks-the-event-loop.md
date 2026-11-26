---
title: "Hashing and reading a large input file blocks the event loop"
status: open
area: core
filed: 2026-11-26
scope: M
verify: test
---

`FileSource`, `LinesSource`, `JSONLSource` and `DirSource` hash their files in `content_hash`,
and the first three read them in `resolve`. On the async path both run on the event loop.
Measured on 0.3.44: a 300 MB `FileSource` stalled the loop for 2.7 s.

`content_hash` is sync and is called from many places, so this needs a design: for example
hashing the sources in a thread before the levels are planned.

Done when: a test shows that another task runs while a source is hashed and read.
