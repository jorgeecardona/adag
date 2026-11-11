"""Every public name of the first import (b62b906) still resolves.

Consumers ported from the internal package import these names. ``KEPT_REMOVED`` lists the few that
are gone on purpose, each with its replacement in the CHANGELOG migration section.
"""

import importlib

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TOP_LEVEL = [
    "SVG",
    "AsyncExecutor",
    "AsyncStep",
    "BatchItem",
    "BatchStep",
    "BranchDispatcher",
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

# (module, attribute path) pairs below the top level.
DEEP = [
    ("adag.parameter", "Parameter"),
    ("adag.step_mode", "StepMode"),
    ("adag.dispatch", "BranchExecutionError"),
    ("adag.hashing", "hash_scalar"),
    ("adag.hashing", "hash_list"),
    ("adag.step", "Step.produces"),
    ("adag.step", "Step.accepts"),
    ("adag.step", "Step.accepts_from"),
    ("adag.base", "CachedStep.output_path"),
    ("adag.base", "CachedStep.is_cached"),
    ("adag.base", "CachedStep.is_partial"),
    ("adag.base", "CachedStep.save"),
    ("adag.base", "CachedStep.load"),
    ("adag.base", "CachedStep.validate"),
    ("adag.base", "CachedStep.metadata"),
    ("adag.deferred", "NullObserver.on_embedding"),
    ("adag.deferred", "NullObserver.on_model_refused"),
    ("adag.deferred", "InlineDispatcher.dispatch"),
    ("adag.dispatch", "SubprocessDispatcher.dispatch"),
]

# Gone on purpose: BatchDispatcher (AWS-only, its worker script was never public; use
# RemoteDispatcher + a JobBackend) and StepOutput (nothing ever returned one).
KEPT_REMOVED = {"BatchDispatcher", "adag.output.StepOutput"}

# Not restored yet. Each 0.3.0 commit removes the names it restores; empty when done.
MISSING: set[str] = {
    "BranchExecutionError",
    "Parameter",
    "StepMode",
    "adag.base.CachedStep.output_path",
    "adag.base.CachedStep.is_cached",
    "adag.base.CachedStep.is_partial",
    "adag.base.CachedStep.save",
    "adag.base.CachedStep.load",
    "adag.base.CachedStep.validate",
    "adag.base.CachedStep.metadata",
    "adag.deferred.NullObserver.on_embedding",
    "adag.deferred.NullObserver.on_model_refused",
    "adag.deferred.InlineDispatcher.dispatch",
    "adag.dispatch.SubprocessDispatcher.dispatch",
}


def _resolve(module: str, path: str) -> object:
    obj: object = importlib.import_module(module)
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _case(label: str):
    marks = [pytest.mark.xfail(strict=True, reason="restored later in 0.3.0")]
    return pytest.param(label, marks=marks if label in MISSING else [], id=label)


@pytest.mark.parametrize("name", [_case(n) for n in TOP_LEVEL])
def test_top_level_name_resolves(name: str) -> None:
    import adag

    assert getattr(adag, name) is not None


@pytest.mark.parametrize("label", [_case(f"{m}.{a}") for m, a in DEEP])
def test_deep_name_resolves(label: str) -> None:
    module, _, path = label.partition(".")
    # Module names have one dot level here ("adag.x"); split after it.
    head, _, rest = path.partition(".")
    _resolve(f"{module}.{head}", rest)  # raises AttributeError when missing
