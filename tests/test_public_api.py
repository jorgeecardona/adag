"""The top-level ``adag`` namespace: a fixed list, so a change to it is a deliberate decision.

It includes every top-level name of the first import (b62b906); see ``test_compat_b62b906``."""

import adag

EXPECTED = {
    "AdagDeprecationWarning",
    "SVG",
    "AsyncStep",
    "BatchItem",
    "BatchStep",
    "CachedStep",
    "ComponentKind",
    "KeyComponent",
    "KeyExplanation",
    "Config",
    "CyclicPipelineError",
    "DirSource",
    "FanOutBranchError",
    "FanOutStep",
    "FileSource",
    "InputParam",
    "ItemCache",
    "JSONLSource",
    "LinesSource",
    "LoggingObserver",
    "NullObserver",
    "Pipeline",
    "PipelineObserver",
    "RunTreeObserver",
    "SerializableStep",
    "Step",
    "StepResult",
    "StepTimeout",
    "SvgOptions",
    "UnboundInputError",
    "UnhashableValueError",
    "WriteJSONL",
    "dag",
    "AsyncExecutor",
    "BranchDispatcher",
    "BranchPolicy",
    "BranchRef",
    "Deferred",
    "Executor",
    "InlineDispatcher",
    "LocalAsyncExecutor",
    "LocalExecutor",
    "Progress",
    "ResolveContext",
    "SubprocessDispatcher",
    "current_observer",
    "observing",
    "render_pipeline_svg",
    "walk",
    "aresolve_all",
    "JsonValue",
    "Edge",
    "Node",
    "NodeMetadata",
    "compute_state",
    "node_metadata",
    "render_graph_svg",
}


def test_public_names_are_the_expected_set():
    assert set(adag.__all__) == EXPECTED


def test_every_public_name_resolves():
    for name in adag.__all__:
        assert getattr(adag, name) is not None
