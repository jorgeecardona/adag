---
title: Overlapping writers of a directory result can break each other
status: open
area: core
owner: null
filed: 2026-11-19
needs:
  - FX-8
scope: M
verify: test
---

Two writers of one directory result (save_result) can overlap when a rerun starts while an orphan job is still running. musil model of the cache write protocol: both pass the remove step and the second rename fails with ENOTEMPTY, so the worker fails; a second writer unlinks .done after the first finished, so is_cached then load raises ValueError; an rmtree by the second writer leaves .done next to a half-deleted directory. Not reproduced in the real code (150 runs, 6 writers): the window is a few syscalls. Text results are not affected.

Fix: skip the save when .done exists, load with one try_load, and accept the other writer's result on ENOTEMPTY (_save_path in src/adag/step.py).