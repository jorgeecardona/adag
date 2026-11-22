---
title: "Compact layering: a graph with many levels is tall"
status: open
area: core
filed: 2026-11-22
scope: M
verify: inspect
---

Every level of the graph is a row. The namecheck graph (4 inputs, 6 steps, one verdict) is about
1360 px tall, because the longest path has 7 levels and each row needs a gap for its edges.

Ideas: put a step with no inputs on the same row as its consumer's other inputs, let two steps that
do not depend on each other share a row when their edges allow it, and cut the gap under a row that
has no sideways edge. The structural tests in `tests/test_svg_layout.py` bound the canvas area and
the aspect ratio, so a better layering can lower those bounds.
