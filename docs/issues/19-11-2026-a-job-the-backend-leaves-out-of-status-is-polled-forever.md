---
title: A job the backend leaves out of status() is polled forever
status: done
area: core
owner: null
filed: 2026-11-19
fixed_in: 0db96fa
needs:
  - FX-5
scope: S
verify: test
---

RemoteDispatcher read a job missing from the status() answer as PENDING, and queue_timeout_seconds defaults to None, so a backend that dropped a job kept the run waiting forever (dispatch.py, dispatch_all). Found by a musil model of the dispatcher: submit, backend drops the job, dead end.

Fix: a job missing for BranchPolicy.missing_status_polls polls in a row (default 3) counts as FAILED, is cancelled, and the retry policy applies. Test: tests/test_remote_dispatcher.py::test_a_job_the_backend_never_reports_fails_after_a_few_polls.