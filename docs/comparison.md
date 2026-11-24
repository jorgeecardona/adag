# adag and other DAG tools

Use this page to decide if adag fits. It compares how each tool decides to rerun a task.
Facts are from each tool's docs, checked on 2026-11-11; links at the end.

## What adag does

- Runs in your Python process: `Pipeline(...).execute()`. No scheduler, server or UI.
- Reruns a step only when its cache key changes. The key hashes the step's fields, its
  inputs (upstream keys, `InputParam` values, file content), and the step class code.
- Caches each item of a `BatchStep` on its own, so a rerun resumes after a partial failure.
- Renders the graph to SVG with cache state, from the library.

## How other tools decide to rerun

| Tool | Rerun rule | Code edit reruns? | Runs in-process | Per-item cache | Graph to SVG |
|---|---|---|---|---|---|
| Luigi | output file is missing | no | yes (local scheduler) | no | no (web UI) |
| Airflow | schedule and task state | no | no (scheduler + database) | no | no (web UI) |
| Prefect 3 | cache policy | opt-in (`TASK_SOURCE`) | yes | no | no |
| Dagster | asset staleness | opt-in (manual `code_version`) | partly (UI-driven) | no | no (web UI) |
| Kedro | output dataset is missing | no (plugin) | yes | no | separate Kedro-Viz |
| joblib.Memory | arguments + function source | yes | yes | no | no |
| Hamilton | node source + upstream data | yes | yes | no | yes |
| redun | task source + inputs | yes | yes | yes (`map_`) | `redun viz` |

## When to use something else

- You need scheduling, a web UI, or retries for every task across machines: use Airflow, Dagster
  or Prefect. adag retries only fan-out branches (see [Dispatch](dispatch.md)).
- You cache single functions, not a graph: joblib.Memory is enough.
- Your graph is pandas columns defined by function names: Hamilton fits that model.
- You want the same source-and-input caching with per-item caching, plus a larger project
  behind it: redun is the closest tool to adag.

## When adag fits

- A data or ML pipeline in one repo, rerun many times while you change code.
- Steps as frozen dataclasses with typed pydantic results, checked by pyright.
- Expensive per-item calls (LLM or API calls) where a failure must not lose finished items.
- You want the cache and the graph picture without running a service.

## Shared limit

adag, joblib, Hamilton and redun hash the task's own source only. An edit to a helper
function the task calls does not rerun it. See [Caching](caching.md#limits).

## Sources

- Luigi: <https://luigi.readthedocs.io/en/stable/workflows.html>,
  <https://luigi.readthedocs.io/en/stable/running_luigi.html>
- Airflow: <https://airflow.apache.org/docs/apache-airflow/stable/>
- Prefect: <https://docs.prefect.io/3.0/develop/task-caching>
- Dagster: <https://docs.dagster.io/guides/dagster/asset-versioning-and-caching>
- Kedro: <https://linen-slack.kedro.org/t/16245766>
- joblib: <https://joblib.readthedocs.io/en/latest/memory.html>
- Hamilton: <https://hamilton.apache.org/concepts/caching/>
- redun: <https://insitro.github.io/redun/tasks.html>, <https://insitro.github.io/redun/visualization.html>
