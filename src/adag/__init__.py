"""adag: a cached, content-addressed DAG engine for Python data pipelines.

This module exports every public name of the package, including the lower-level pieces
(executors, dispatchers, ``ResolveContext``, ``walk``).
"""

__version__ = "0.3.17"

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
from adag.deprecation import AdagDeprecationWarning
from adag.dir_source import DirSource
from adag.dispatch import BranchPolicy, SubprocessDispatcher
from adag.fan_out import FanOutBranchError, FanOutStep
from adag.hashing import UnhashableValueError
from adag.io import FileSource, JSONLSource, LinesSource
from adag.item_cache import ItemCache
from adag.key import ComponentKind, Expansion, KeyComponent, KeyExplanation
from adag.loop import LoopNotConverged, LoopStep
from adag.observers import LoggingObserver, RunTreeObserver
from adag.pipeline import (
    CyclicPipelineError,
    JsonValue,
    Pipeline,
    UnboundInputError,
    aresolve_all,
)
from adag.serializable import SerializableStep
from adag.sinks import WriteJSONL
from adag.step import AsyncStep, Step, StepResult, StepTimeout
from adag.template import Template
from adag.visualization import (
    SVG,
    Edge,
    Node,
    NodeMetadata,
    SvgOptions,
    compute_state,
    dag,
    node_metadata,
    render_graph_svg,
    render_pipeline_svg,
    walk,
)

__all__ = [
    "SVG",
    "AdagDeprecationWarning",
    "AsyncExecutor",
    "AsyncStep",
    "BatchItem",
    "BatchStep",
    "BranchDispatcher",
    "BranchPolicy",
    "BranchRef",
    "CachedStep",
    "ComponentKind",
    "Config",
    "CyclicPipelineError",
    "Deferred",
    "DirSource",
    "Edge",
    "Executor",
    "Expansion",
    "FanOutBranchError",
    "FanOutStep",
    "FileSource",
    "InlineDispatcher",
    "InputParam",
    "ItemCache",
    "JSONLSource",
    "JsonValue",
    "KeyComponent",
    "KeyExplanation",
    "LinesSource",
    "LocalAsyncExecutor",
    "LocalExecutor",
    "LoggingObserver",
    "LoopNotConverged",
    "LoopStep",
    "Node",
    "NodeMetadata",
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
    "Template",
    "UnboundInputError",
    "UnhashableValueError",
    "WriteJSONL",
    "aresolve_all",
    "compute_state",
    "current_observer",
    "dag",
    "node_metadata",
    "observing",
    "render_graph_svg",
    "render_pipeline_svg",
    "walk",
]

# name -> (module, attribute, replacement named in the warning)
_DEPRECATED = {
    "Parameter": ("adag.parameter", "Parameter", "InputParam(name, value_type=T)"),
    "StepMode": ("adag.step_mode", "StepMode", "your own enum (adag never reads it)"),
    "BranchExecutionError": (
        "adag.dispatch",
        "BranchExecutionError",
        "FanOutBranchError (branch failures are raised as that)",
    ),
}


def __getattr__(name: str) -> object:
    """Resolve deprecated top-level names on first use, so ``import adag`` does not warn."""
    if name in _DEPRECATED:
        import importlib

        from adag.deprecation import warn_deprecated

        module, attr, replacement = _DEPRECATED[name]
        warn_deprecated(f"adag.{name}", replacement)
        return getattr(importlib.import_module(module), attr)
    raise AttributeError(f"module 'adag' has no attribute {name!r}")
