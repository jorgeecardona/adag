---
title: "SubprocessDispatcher with a policy polls local branches every 5 s"
status: open
area: core
filed: 2026-11-11
scope: XS
verify: test
---

`SubprocessDispatcher()` polls every 0.05 s, but `SubprocessDispatcher(BranchPolicy(max_retries=1))`
polls every 5 s: the 0.05 default applies only when no policy is passed
([src/adag/dispatch.py](../../src/adag/dispatch.py)). Adding retries slows a local run by up to
5 s per poll.

Fix: make `poll_interval_seconds` default to `None`, and let each dispatcher pick its own value
when it is `None`. Test: a policy without `poll_interval_seconds` polls at 0.05 s locally.
