"""``explain_key``: every component of a cache key, and the same key the cache uses."""

from pathlib import Path

from adag import ComponentKind, FileSource, KeyExplanation, Pipeline
from test_key_golden import (
    Down,
    Labelled,
    Pinned,
    Plain,
    WithConfig,
    WithFile,
    WithNested,
    _graph,
)


def _explanations(data_file: Path) -> list[tuple[KeyExplanation, str | None]]:
    graphs = [
        _graph(),
        Plain()(),
        Pinned()(),
        WithConfig()(),
        Labelled(node_label="shown")(),
        WithFile(data=FileSource(str(data_file)))(),
        WithNested(inner=Plain())(),
    ]
    params = {"p": 5}
    return [(g.explain_key(params), g.content_hash(params=params)) for g in graphs]


def test_explanation_key_equals_content_hash(tmp_path: Path) -> None:
    data_file = tmp_path / "data.txt"
    data_file.write_text("golden\n")
    for explanation, key in _explanations(data_file):
        assert explanation.key == key


def test_changing_one_field_changes_only_that_component() -> None:
    a = {c.name: c for c in Down()(up=Plain(a=1)(), p=1, q=2, raw="r").explain_key().components}
    b = {c.name: c for c in Down()(up=Plain(a=2)(), p=1, q=2, raw="r").explain_key().components}
    changed = [name for name in a if a[name].digest != b[name].digest]
    assert changed == ["up"]
    a2 = {(c.kind, c.name): c for c in Plain(a=1)().explain_key().components}
    b2 = {(c.kind, c.name): c for c in Plain(a=2)().explain_key().components}
    assert [k for k in a2 if a2[k].digest != b2[k].digest] == [(ComponentKind.FIELD, "a")]


def test_config_and_label_are_listed_as_skipped() -> None:
    kinds = {c.name: c.kind for c in WithConfig()().explain_key().components}
    assert kinds["workers"] is ComponentKind.SKIPPED
    kinds = {c.name: c.kind for c in Labelled(node_label="x")().explain_key().components}
    assert kinds["node_label"] is ComponentKind.SKIPPED


def test_missing_file_blocks_the_key_and_names_the_field(tmp_path: Path) -> None:
    explanation = WithFile(data=FileSource(str(tmp_path / "missing.txt")))().explain_key()
    assert explanation.key is None
    assert explanation.blocked_by == "data"


def test_upstream_component_names_the_upstream_step_and_key() -> None:
    up = Plain()()
    components = {c.name: c for c in Down()(up=up, p=1, q=2, raw="r").explain_key().components}
    assert components["up"].kind is ComponentKind.UPSTREAM
    assert components["up"].value == f"Plain {up.content_hash()}"


def test_dict_round_trip_and_text_form() -> None:
    explanation = _graph().explain_key({"p": 5})
    assert KeyExplanation.from_dict(explanation.to_dict()) == explanation
    text = str(explanation)
    assert text.splitlines()[0].startswith(f"Down  key={explanation.key}")
    assert "param" in text and "upstream" in text


def test_pipeline_explains_every_node_upstream_first() -> None:
    explanations = Pipeline(_graph()).explain_keys({"p": 5})
    assert [e.step for e in explanations] == ["Plain", "Down"]
