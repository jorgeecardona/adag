---
title: "Highlight the inputs and consumers of a node on hover"
status: open
area: docs
filed: 2026-11-22
scope: M
verify: inspect
---

In a big graph a reader wants to see where one node comes from and where it goes. Hovering a node
could dim every node that is not an ancestor or a consumer of it.

This needs CSS `:has()` and one rule per node, and it works only when the SVG is inline in the
page. It never works in a README image. Each node group already has `data-name`, so a host page can
write the rule today. A built-in version would need the ancestors listed as `data-` attributes.
