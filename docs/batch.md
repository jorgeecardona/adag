# Batch steps

Use `BatchStep` when a step does the same expensive call for many items. Each item's result
is cached on its own, so a rerun after a failure computes only the missing items.

Implement three methods:

1. `items(**inputs)`: return one `BatchItem(key=..., data=...)` per item.
2. `process_item(item, **inputs)`: compute one item.
3. `assemble(results, **inputs)`: combine the per-item results into the step result.

Instead of `items()`, you can implement `items_data()` and return plain data; adag then
derives each key from the step's cache key and the item data. Override `process_batch()` to
process items concurrently.

```python
--8<-- "docs/examples/batch.py"
```

```text
first run failed: API error for carol | called: ['alice', 'bob', 'carol']
second run called: ['carol', 'dave']
['hello alice', 'hello bob', 'hello carol', 'hello dave']
```

```mermaid
flowchart TB
    items["items(): one BatchItem per item"] --> each{"Item result saved?"}
    each -- yes --> load["Load it"]
    each -- no --> proc["process_item(): compute it"] --> save["Save it right away"]
    load --> asm["assemble(): combine all results"]
    save --> asm
    asm --> done(["Step result"])
```

A crash loses only the item in progress: items already saved are loaded on the next run.

Item results live under `<cache_dir>/.items/<namespace>/`. The namespace is the class name in
snake case (`greet` above).
