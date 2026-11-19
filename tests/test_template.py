"""``Template``: build over placeholders to hash, build over values to run."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from adag import Step, StepResult, Template
from adag.hashing import Placeholder, TemplateInputError, encode_value


class Size(BaseModel):
    chars: int


@dataclass(frozen=True)
class Measure(Step[Size]):
    text: str

    def run(self, text: str) -> Size:
        return Size(chars=len(text))


def _measure(key: str, item: str) -> StepResult[Size]:
    return Measure(text=item)()


def _fixed_a(key: str, item: str) -> StepResult[Size]:
    return Measure(text="a")()


def _fixed_b(key: str, item: str) -> StepResult[Size]:
    return Measure(text="b")()


def _not_a_graph(key: str, item: str) -> Any:
    return 1


def test_template_hash_ignores_the_items() -> None:
    template = Template(build=_measure)
    assert template.hash() == Template(build=_measure).hash()
    assert template.hash() is not None


def test_template_hash_changes_with_the_input_names() -> None:
    assert Template(build=_measure).hash() != Template(build=_measure, inputs=("k", "v")).hash()


def test_template_hash_changes_with_a_field_the_build_sets() -> None:
    assert Template(build=_fixed_a).hash() != Template(build=_fixed_b).hash()


def test_branch_key_is_the_key_of_the_graph_built_over_that_item() -> None:
    template = Template(build=_measure)
    assert template.bind("a", "same").content_hash() == template.bind("b", "same").content_hash()
    assert template.bind("a", "one").content_hash() != template.bind("a", "two").content_hash()


def _attribute(item: Any) -> object:
    return item.upper()


def _add(item: Any) -> object:
    return item + "x"


def _equal(item: Any) -> object:
    return item == "a"


def _format(item: Any) -> object:
    return f"{item}"


def _subscript(item: Any) -> object:
    return item["x"]


def _hash(item: Any) -> object:
    return {item: 1}


USES: list[Callable[[Any], object]] = [
    _attribute,
    len,
    _add,
    _equal,
    _format,
    str,
    bool,
    list,
    _subscript,
    _hash,
]


@pytest.mark.parametrize("use", USES, ids=lambda use: use.__name__)
def test_a_build_that_uses_the_value_of_an_input_raises(use: Callable[[Any], object]) -> None:
    def build(key: str, item: Any) -> StepResult[Size]:
        use(item)
        return Measure(text="x")()

    with pytest.raises(TemplateInputError, match="template input 'item'"):
        Template(build=build).trace()


def test_a_placeholder_encodes_by_name_and_has_a_readable_repr() -> None:
    assert encode_value(Placeholder("item")) != encode_value(Placeholder("key"))
    assert repr(Placeholder("item")) == "<template input 'item'>"


def test_build_must_return_a_step_result() -> None:
    with pytest.raises(TypeError, match="must return a StepResult"):
        Template(build=_not_a_graph).trace()


def test_bind_checks_the_number_of_values() -> None:
    with pytest.raises(TypeError, match="takes 2 input"):
        Template(build=_measure).bind("only one")


def test_a_build_that_reads_the_value_of_an_input_raises() -> None:
    def build(key: str, item: Any) -> StepResult[Size]:
        return Measure(text=item.upper())()

    with pytest.raises(TemplateInputError, match="template input 'item'"):
        Template(build=build).hash()
