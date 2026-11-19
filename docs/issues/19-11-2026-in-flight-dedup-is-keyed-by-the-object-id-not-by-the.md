---
title: In-flight dedup is keyed by the object id, not by the cache key
status: done
area: core
owner: null
filed: 2026-11-19
fixed_in: 2a58341
needs:
  - FX-7
scope: S
verify: test
---

The in-flight registries in ResolveContext were keyed by id(node) (step.py, resolve and aresolve). Two node objects with the same content key both ran their body when threads reached them together. A template fan-out whose branches each build their own upstream node hits this. Reproduced: 8 runs with max_workers=8.

Fix: key the registries by the content key when the run has a result store, else by the id. The second node finds the first one's result in the cache. Tests: tests/test_inflight_dedup.py::test_equal_nodes_built_per_branch_run_the_body_once and test_equal_nodes_gathered_on_the_loop_run_the_body_once.