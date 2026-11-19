---
title: The dispatcher ignores the cache while its own job is live
status: done
area: core
owner: null
filed: 2026-11-19
fixed_in: 0db96fa
needs:
  - FX-6
scope: S
verify: test
---

RemoteDispatcher never looked in the cache for a branch whose job was queued, running or failed (dispatch.py, _advance and fail). Found by a musil model: submit, crash, rerun, submit again, the old job succeeds, the new job times out in the queue, and the rerun raises FanOutBranchError although the result is cached.

Fix: on every poll and inside fail(), read the cache once with StepResult.try_load. A cached result ends the branch and cancels the job. A result that appears after the failure is decided still counts as failed. Tests: tests/test_remote_dispatcher.py::test_a_failing_job_succeeds_when_another_process_wrote_the_result and test_a_queue_timeout_succeeds_when_the_result_is_already_cached.