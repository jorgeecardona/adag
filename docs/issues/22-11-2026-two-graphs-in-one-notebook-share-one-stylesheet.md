---
title: "Two graphs in one notebook page share one stylesheet"
status: open
area: core
filed: 2026-11-22
scope: S
verify: inspect
---

Classic Jupyter puts the SVG inline, so its `<style>` element applies to the whole page. A light
graph and a dark graph in the same notebook both take the rules of the last one drawn.

Scoping the rules to the graph (for example with `:where(.dag--dark)`) breaks renderers that do
not know `:where`. The `SvgOptions(theme="auto")` sheet has the same problem.

Fix to try: give each graph a unique class and prefix every rule with it. That changes every
selector in `src/adag/svg_theme.py`, so the host-facing class names need a decision first.
