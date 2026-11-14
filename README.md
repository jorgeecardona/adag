# adag

[![pipeline status](https://gitlab.com/cardona-oss/adag/badges/main/pipeline.svg)](https://gitlab.com/cardona-oss/adag/-/pipelines)
[![PyPI version](https://img.shields.io/pypi/v/adag.svg)](https://pypi.org/project/adag/)
[![Python versions](https://img.shields.io/pypi/pyversions/adag.svg)](https://pypi.org/project/adag/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://gitlab.com/cardona-oss/adag/-/blob/main/LICENSE)

Another DAG: a cached, content-addressed DAG engine for Python data pipelines.

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

A step's cache key is a hash of its fields, its inputs, and its source code. Change any of
them and that step and everything downstream recompute; nothing else does. Use it for data
and ML pipelines where steps are slow and you rerun the graph often. How it compares to Luigi,
Airflow, Prefect, Dagster, Hamilton and redun: [Why adag](https://cardona-oss.gitlab.io/adag/comparison/).

## Key features

- Content-addressed cache: a code edit invalidates only the steps it touches.
- `Config` fields stay out of the cache key (worker counts, clients, timeouts).
- Sync and async steps (`Step`, `AsyncStep`), with a parallel executor.
- `BatchStep` caches each item on its own, so a rerun resumes after a failure.
- `FanOutStep` runs a sub-graph per key.
- Cache stores for step results and `BatchStep` items: filesystem, memory, SQLite, SQLAlchemy
  (`adag[sql]`), Redis (`adag[redis]`). Pass one with `Pipeline(..., store=)`.
- `explain_key` lists every part of a cache key, so you can see why a step ran again.
- `save_result` and `load_result` cache a result in your own format (JSONL, a directory).
- Save a pipeline to disk and load it back with identical hashes.
- SVG rendering of the graph, and an `adag run pipeline.py` CLI.
- Run fan-out branches in worker processes or on a job scheduler (AWS Batch, SLURM, Kubernetes):
  write `submit`, `status` and `cancel`; adag does retries, timeouts and cancel.

## Links

- **Documentation:** https://cardona-oss.gitlab.io/adag/
- **Source:** https://gitlab.com/cardona-oss/adag
- **Issues:** https://gitlab.com/cardona-oss/adag/-/work_items
- **PyPI:** https://pypi.org/project/adag/
- **Changelog:** [CHANGELOG.md](https://gitlab.com/cardona-oss/adag/-/blob/main/CHANGELOG.md)

## Contributing

Developer setup, the dev loop, and the release process live in
[CONTRIBUTING.md](https://gitlab.com/cardona-oss/adag/-/blob/main/CONTRIBUTING.md). In short: `uv sync && make install-hooks`, then
`make check` runs the same gate CI runs.

## License

MIT © 2026 Jorge Cardona (see [LICENSE](https://gitlab.com/cardona-oss/adag/-/blob/main/LICENSE)).
