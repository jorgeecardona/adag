<h1 align="center">adag</h1>

<p align="center"><strong>Build data and ML pipelines in Python where each step caches its output
by content: edit one step's code and only that step and its dependants rerun.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/adag/"><img alt="PyPI" src="https://img.shields.io/pypi/v/adag?style=flat-square"></a>
  <a href="https://pypi.org/project/adag/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/adag?style=flat-square"></a>
  <a href="https://cardona-oss.gitlab.io/adag/"><img alt="Docs" src="https://img.shields.io/badge/docs-cardona--oss.gitlab.io%2Fadag-blue?style=flat-square"></a>
  <a href="https://gitlab.com/cardona-oss/adag/-/pipelines"><img alt="Pipeline" src="https://img.shields.io/gitlab/pipeline-status/cardona-oss%2Fadag?branch=main&style=flat-square"></a>
  <a href="https://gitlab.com/cardona-oss/adag/-/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square"></a>
</p>

<p align="center">
  <a href="https://cardona-oss.gitlab.io/adag/comparison/">Why adag</a> ·
  <a href="https://cardona-oss.gitlab.io/adag/steps/">Steps and pipelines</a> ·
  <a href="https://cardona-oss.gitlab.io/adag/api/">API reference</a> ·
  <a href="https://gitlab.com/cardona-oss/adag/-/blob/main/CHANGELOG.md">Changelog</a>
</p>

---

## Overview

adag is a DAG engine for Python data and ML pipelines. Each step is a frozen dataclass; its
cache key is a hash of its fields, its inputs, and its source code. Run the pipeline a second
time and adag reads every unchanged result from cache without calling `run` again. Change a
step's code or a field value and only that step and everything downstream of it recomputes.

Use it for pipelines where individual steps are slow and you rerun the graph often: model
training, data preprocessing, feature extraction. Write steps in plain Python and run the
pipeline in the same process. `explain_key` shows why a step ran again.

Status: beta.

## Install

```bash
pip install adag
```

Requires Python 3.12 or newer. CI runs the tests on 3.12 and 3.13.

## Quickstart

Each step is a frozen dataclass. Call a step to wire it into the graph, then run the
graph with a `Pipeline`.

```python
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import InputParam, Pipeline, Step


class Number(BaseModel):
    value: int


@dataclass(frozen=True)
class Square(Step[Number]):
    def run(self, x: int) -> Number:
        print("computing square")
        return Number(value=x * x)


@dataclass(frozen=True)
class AddOne(Step[Number]):
    def run(self, n: Number) -> Number:
        return Number(value=n.value + 1)


x = InputParam[int]("x")
total = AddOne()(n=Square()(x=x))
pipeline = Pipeline(total, cache_dir=Path("cache"))

print(pipeline.execute(x=4))  # computes both steps
print(pipeline.execute(x=4))  # served from the cache
```

The second run reads both results from `cache/` and does not call `Square.run` again:

```text
computing square
value=17
value=17
```

(output trimmed: structlog lines removed)

## Why adag

Use it for data and ML pipelines where steps are slow and you rerun the graph often.

Luigi and Airflow do not rerun a step when its source code changes. Prefect 3 does so only
with opt-in cache-key configuration. Hamilton hashes source code but its model is column-based
(functions as DAG nodes), not arbitrary Python classes. redun is the closest alternative: it
also caches by task source and inputs, and adds per-item caching with `map_`.

Use adag when you want steps as frozen dataclasses with typed pydantic results, checked by
pyright, and the cache and the graph picture without running a service. Use Airflow, Dagster or
Prefect when you need scheduling, a web UI, or retries for every task across machines. Use redun
when you want a larger project behind the same kind of caching. See the full comparison:
[Why adag](https://cardona-oss.gitlab.io/adag/comparison/).

## Key features

- Content-addressed cache: a code edit invalidates only the steps it touches.
- `Config` fields stay out of the cache key (worker counts, clients, timeouts).
- Sync and async steps (`Step`, `AsyncStep`), with a parallel executor.
- `BatchStep` caches each item on its own, so a rerun resumes after a failure.
- `FanOutStep` runs a sub-graph per key.
- `LoopStep` runs a sub-graph in rounds, each feeding the next, until a condition holds.
  Each round is cached.
- `runs_on = "thread"` or `"process"` on a step: thread steps keep the event loop free; process
  steps run CPU-bound code in workers.
- Cache stores for step results and `BatchStep` items: filesystem, memory, SQLite, SQLAlchemy
  (`adag[sql]`), Redis (`adag[redis]`). Pass one with `Pipeline(..., store=)`.
- `explain_key` lists every part of a cache key, so you can see why a step ran again.
- `save_result` and `load_result` cache a result in your own format (JSONL, a directory).
- Save a pipeline to disk and load it back with identical hashes.
- SVG rendering of the graph, and an `adag run pipeline.py` CLI.
- Run fan-out branches in worker processes or on a job scheduler (AWS Batch, SLURM, Kubernetes):
  write `submit`, `status` and `cancel`; adag does retries, timeouts and cancel.

## Limits

- Only the step's own class body is hashed. An edit to a base class, a helper function or a
  module constant does not rerun the step.
- No scheduler, server or UI. adag retries only fan-out branches.
- No backend for a specific scheduler ships. `CommandBackend` takes the submit, status and
  cancel commands of your scheduler's CLI; for anything else, write a `JobBackend`.
- Cache stores: filesystem, memory, SQLite, SQLAlchemy and Redis only.
- Status is beta. The API can change between releases.

## Links

- Documentation: <https://cardona-oss.gitlab.io/adag/>
- Repository: <https://gitlab.com/cardona-oss/adag>
- Issues: <https://gitlab.com/cardona-oss/adag/-/work_items>
- PyPI: <https://pypi.org/project/adag/>
- Changelog: [CHANGELOG.md](https://gitlab.com/cardona-oss/adag/-/blob/main/CHANGELOG.md)
- Find a feature: <https://cardona-oss.gitlab.io/adag/comparison/#find-a-feature>

## Contributing

Developer setup, the dev loop, and the release process live in
[CONTRIBUTING.md](https://gitlab.com/cardona-oss/adag/-/blob/main/CONTRIBUTING.md). In short:
`uv sync && make install-hooks`, then `make check` runs the same gate CI runs.

## License

MIT © 2026 Jorge Cardona (see [LICENSE](https://gitlab.com/cardona-oss/adag/-/blob/main/LICENSE)).
