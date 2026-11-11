# Visualization

`dag(output)` renders the graph to SVG. With `cache_dir`, each step is colored by its state:
cached, pending, or blocked (an input is missing).

```python
--8<-- "docs/examples/visualization.py"
```

![The quickstart graph](assets/graph.svg)

In a notebook, the returned `SVG` object displays inline. Use `SvgOptions` to change the theme
or hide the per-step parameters.
