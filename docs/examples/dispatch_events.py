"""A dispatched branch reports its steps to the main observer while it runs."""

import tempfile
from pathlib import Path
from typing import Any

from adag import NullObserver, Pipeline
from adag.cli import load_pipeline_module
from adag.dispatch import SubprocessDispatcher


class Watcher(NullObserver):
    """Keeps the step_start events each branch sent."""

    def __init__(self) -> None:
        self.names: dict[str, list[str]] = {}

    def on_event(self, event: str, fields: dict[str, Any]) -> None:
        if event == "branch.step_start":
            self.names.setdefault(fields["key"], []).append(event)


module = load_pipeline_module(Path(__file__).with_name("dispatch_events_pipeline.py"))
pipeline = Pipeline(module.per_key, cache_dir=Path(tempfile.mkdtemp()))

watcher = Watcher()
pipeline.execute(observer=watcher, dispatcher=SubprocessDispatcher())
for key in sorted(watcher.names):
    print(key, watcher.names[key])
