"""SubprocessDispatcher with a fan-out over an upstream step: one worker process per part."""

import os
import tempfile
from pathlib import Path

from adag import NullObserver, Pipeline
from adag.cli import load_pipeline_module
from adag.dispatch import SubprocessDispatcher

module = load_pipeline_module(Path(__file__).with_name("dispatch_template_pipeline.py"))
pipeline = Pipeline(module.sizes, cache_dir=Path(tempfile.mkdtemp()))

for text in ["Hello.;A longer paragraph.;Bye.", "Hello.;A longer paragraph.;See you."]:
    result = pipeline.execute(observer=NullObserver(), dispatcher=SubprocessDispatcher(), text=text)
    print({key: size.chars for key, size in result.items()})
    print("ran in other processes:", all(size.pid != os.getpid() for size in result.values()))
