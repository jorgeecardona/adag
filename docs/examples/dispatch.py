"""SubprocessDispatcher: each fan-out branch runs in its own process."""

import os
import tempfile
from pathlib import Path

from adag import NullObserver, Pipeline
from adag.cli import load_pipeline_module
from adag.dispatch import SubprocessDispatcher

module = load_pipeline_module(Path(__file__).with_name("dispatch_pipeline.py"))
pipeline = Pipeline(module.per_key, cache_dir=Path(tempfile.mkdtemp()))

result = pipeline.execute(observer=NullObserver(), dispatcher=SubprocessDispatcher())
print({key: r.value for key, r in result.items()})
print("ran in other processes:", all(r.pid != os.getpid() for r in result.values()))
