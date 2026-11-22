---
title: "The router joins and leaves a shared trunk within 12 px"
status: open
area: core
filed: 2026-11-22
scope: M
verify: test
---

Two edges of one source sometimes use different ports. One edge runs sideways onto the trunk of the
other, and the other edge leaves the trunk 12 px lower. In `blueprint` this now draws two dots
close together (a small ladder).

`tests/test_edge_rules.py` counts these as "share a stretch that does not start at the source".
Round corners hid them before. Cases: `ml_training` (blueprint, terminals, normal) and
`layered_40` (blueprint, compact).

Fix to try: give the edges of one source the same port when they share a trunk, so the shared
stretch starts at the source. Then lower the two numbers in `KNOWN`.
