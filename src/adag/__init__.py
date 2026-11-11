"""adag — content-addressable DAG engine for data pipelines.

Core engine:
    Deferred, InputParam, ResolveContext — value abstraction
    PipelineObserver, NullObserver       — instrumentation hook on ResolveContext
    LoggingObserver                      — default observer; logs each step's lifecycle
    RunTreeObserver                      — renders a run as an indented timing tree
    Executor, LocalExecutor              — pluggable step execution on ResolveContext
    AsyncExecutor, LocalAsyncExecutor    — the awaitable counterparts (the async resolve path)
    Step                                — single step base class
    AsyncStep                           — Step whose body awaits I/O; resolve via Pipeline.aexecute()
    CachedStep                          — Step with content-addressable caching
    SerializableStep                    — Step with save/load support
    StepResult                          — deferred result of a Step
    FileSource, LinesSource, JSONLSource — file leaf Deferreds (text / lines / parsed)
    Config, ItemCache                    — input/cache types
    Pipeline                            — DAG executor
    BatchItem, BatchStep                — per-item batch processing with ItemCache
"""

__version__ = "0.1.3"

from adag.base import CachedStep
from adag.batch import BatchItem, BatchStep
from adag.config import Config
from adag.deferred import (
    AsyncExecutor,
    BranchDispatcher,
    BranchRef,
    Deferred,
    Executor,
    InlineDispatcher,
    InputParam,
    LocalAsyncExecutor,
    LocalExecutor,
    NullObserver,
    PipelineObserver,
    Progress,
    ResolveContext,
    current_observer,
    observing,
)
from adag.dir_source import DirSource
from adag.dispatch import BranchExecutionError, BranchPolicy, SubprocessDispatcher
from adag.fan_out import FanOutBranchError, FanOutStep
from adag.io import FileSource, JSONLSource, LinesSource
from adag.item_cache import ItemCache
from adag.observers import LoggingObserver, RunTreeObserver
from adag.pipeline import CyclicPipelineError, Pipeline, UnboundInputError
from adag.serializable import SerializableStep
from adag.sinks import WriteJSONL
from adag.step import AsyncStep, Step, StepResult, StepTimeout
from adag.visualization import SVG, SvgOptions, dag, render_pipeline_svg, walk

__all__ = [
    "SVG",
    "AsyncExecutor",
    "AsyncStep",
    "BatchItem",
    "BatchStep",
    "BranchDispatcher",
    "BranchExecutionError",
    "BranchPolicy",
    "BranchRef",
    "CachedStep",
    "Config",
    "CyclicPipelineError",
    "Deferred",
    "DirSource",
    "Executor",
    "FanOutBranchError",
    "FanOutStep",
    "FileSource",
    "InlineDispatcher",
    "InputParam",
    "ItemCache",
    "JSONLSource",
    "LinesSource",
    "LocalAsyncExecutor",
    "LocalExecutor",
    "LoggingObserver",
    "NullObserver",
    "Pipeline",
    "PipelineObserver",
    "Progress",
    "ResolveContext",
    "RunTreeObserver",
    "SerializableStep",
    "Step",
    "StepResult",
    "StepTimeout",
    "SubprocessDispatcher",
    "SvgOptions",
    "UnboundInputError",
    "WriteJSONL",
    "current_observer",
    "dag",
    "observing",
    "render_pipeline_svg",
    "walk",
]
