# Visualization

`dag(output)` renders the graph to SVG. With `cache_dir`, each step is colored by its state:
cached, pending, or blocked (an input is missing).

```python
--8<-- "docs/examples/visualization.py"
```

![The quickstart graph](assets/graph.svg)

In a notebook, the returned `SVG` object displays inline. Use `SvgOptions` to change the theme
or hide the per-step parameters.

## Other entry points

- `render_graph_svg(nodes, edges)`: draw a graph from your own `NodeMetadata` list and
  `(source, target)` pairs. The old private name `_render_graph_svg` still works.
- `node_metadata(nodes, cache_dir=, prefix=)`: the `NodeMetadata` `dag` would draw for each
  node from `walk`. Change what you want with `dataclasses.replace`, then pass the list to
  `render_graph_svg`. This is the supported way to customize a drawing; `Node`, `Edge`,
  `NodeMetadata`, `compute_state`, `node_metadata` and `render_graph_svg` are all importable
  from `adag`, and their names and fields stay stable.
- `compute_state(node, cache_dir)`: the state a node is drawn with
  (`CACHED`, `PENDING`, `BLOCKED`, `SOURCE`), read from the node's cache key.
- `dag(...)` returns an `SVG` object. A notebook renders it inline; `str(svg)` is the SVG text.
