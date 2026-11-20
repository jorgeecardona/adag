# adag

Another DAG: a cached, content-addressed DAG engine for Python data pipelines.

A step's cache key is a hash of its fields, its inputs and its source code. Change one of them
and that step and everything downstream recompute. Nothing else does.

## Install

```bash
pip install adag
```

Requires Python 3.12 or newer.

## Start here

1. [Why adag](comparison.md): decide if it fits. 2 minutes.
2. [Steps and pipelines](steps.md): write two steps, run them, see the cache hit.
3. [Inputs](inputs.md): feed values and files into the graph.
4. [Caching](caching.md): what is in a cache key, and what `Config` keeps out.
5. [Command line](cli.md): run a target from a shell.

## The first pipeline

```python
--8<-- "docs/examples/quickstart.py"
```

```text
computing square
value=17
value=17
```

(output trimmed: log lines removed)

The second run reads both results from the cache and does not call `Square.run` again.

## Find a feature

| You want to | Read |
|---|---|
| Keep a setting out of the cache key | [Caching](caching.md#keep-a-field-out-of-the-key-config) |
| See why a step ran again | [Why a step recomputed](cache-keys.md) |
| Cache a result that is not JSON | [Caching](caching.md#results-that-are-not-one-json-document) |
| Resume a long loop after a failure | [Batch steps](batch.md) |
| Run the same sub-graph for each key | [Fan-out](fan-out.md) |
| Run a sub-graph in cached rounds until a condition holds | [Loops](loops.md) |
| Run a step in a thread or worker process | [Async and parallel](async-parallel.md#where-a-step-runs) |
| Await I/O, or run steps at the same time | [Async and parallel](async-parallel.md) |
| Run branches in processes or on a scheduler | [Dispatch](dispatch.md) |
| Keep the cache in SQLite, SQL or Redis | [Cache stores](cache-stores.md) |
| Save a pipeline and load it in another process | [Save and load](save-load.md) |
| Draw the graph | [Visualization](visualization.md) |
| Time a run or receive its events | [Observers](observers.md) |
| Upgrade from 0.1 or 0.2 | [Upgrade to 0.3.0](migration.md) |
| Look up a name | [API reference](api.md) |

## Links

- Source: <https://gitlab.com/cardona-oss/adag>
- Issues: <https://gitlab.com/cardona-oss/adag/-/work_items>
- PyPI: <https://pypi.org/project/adag/>
