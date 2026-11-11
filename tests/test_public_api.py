"""The top-level ``adag`` namespace: a fixed list, so a change to it is a deliberate decision."""

import adag

EXPECTED = {
    "SVG",
    "AsyncStep",
    "BatchItem",
    "BatchStep",
    "CachedStep",
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
}


def test_public_names_are_the_expected_set():
    assert set(adag.__all__) == EXPECTED


def test_every_public_name_resolves():
    for name in adag.__all__:
        assert getattr(adag, name) is not None
