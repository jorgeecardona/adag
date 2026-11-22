---
title: "dag() could also return the graph as Graphviz dot text"
status: open
area: core
filed: 2026-11-22
scope: S
verify: test
---

A user who wants a different layout, or a graph of hundreds of nodes, could send the graph to
Graphviz. A `to_dot(output)` that returns text needs no dependency: adag would write the text and
the user would run `dot`.

The nodes and edges are already in `walk(output)` and `node_metadata`. The text would carry each
step title, state and key as node attributes.
