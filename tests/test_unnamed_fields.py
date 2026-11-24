"""A step method may leave dataclass fields unnamed and read them from ``self`` (#15)."""

import asyncio
import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest
from pydantic import BaseModel

from adag import (
    AsyncStep,
    BatchItem,
    BatchStep,
    Config,
    InputParam,
    NullObserver,
    Pipeline,
    Step,
    UnacceptedInputError,
    UnboundInputError,
)
from golden_batch_steps import Shout


class Text(BaseModel):
    text: str


@dataclass(frozen=True)
class Summarize(Step[Text]):
    """``level`` is a field that ``run`` does not name."""

    level: int = 1

    def run(self, sessions: str) -> Text:
        return Text(text=f"{sessions}:{self.level}")


@dataclass(frozen=True)
class SummarizeAsync(AsyncStep[Text]):
    level: int = 1

    async def arun(self, sessions: str) -> Text:
        return Text(text=f"{sessions}:{self.level}")


@dataclass(frozen=True)
class InChild(Step[Text]):
    runs_on: ClassVar[Literal["loop", "thread", "process"]] = "process"
    level: int = 1

    def run(self, sessions: str) -> Text:
        return Text(text=f"{sessions}:{self.level}:{os.getpid()}")


@dataclass(frozen=True)
class TakesAll(Step[Text]):
    level: int = 1

    def run(self, sessions: str, **fields: Any) -> Text:
        return Text(text=f"{sessions}:{sorted(fields)}")


@dataclass(frozen=True)
class Tuned(Step[Text]):
    level: Config[int] = Config(3)

    def run(self, sessions: str) -> Text:
        return Text(text=f"{sessions}:{self.level.value}")


@dataclass(frozen=True)
class TunedUnwrapped(Step[Text]):
    level: Config[int] = Config(3)

    def run(self, sessions: str, level: int) -> Text:
        return Text(text=f"{sessions}:{level}")


def _run(step: Step[Text], sessions: str = "s") -> Text:
    return Pipeline(step(sessions=InputParam("sessions"))).execute(
        observer=NullObserver(), sessions=sessions
    )


def test_a_field_that_run_does_not_name_is_read_from_self() -> None:
    assert _run(Summarize(level=4)).text == "s:4"


def test_the_unnamed_field_still_changes_the_step_key() -> None:
    assert Summarize(level=1).content_hash() != Summarize(level=2).content_hash()


def test_an_async_step_may_leave_a_field_unnamed() -> None:
    node = SummarizeAsync(level=5)(sessions=InputParam("sessions"))
    result = asyncio.run(Pipeline(node).aexecute(observer=NullObserver(), sessions="s"))
    assert result.text == "s:5"


def test_a_process_step_may_leave_a_field_unnamed() -> None:
    result = _run(InChild(level=6))
    assert result.text.startswith("s:6:")
    assert result.text != f"s:6:{os.getpid()}"


def test_a_method_with_kwargs_still_receives_every_field() -> None:
    assert _run(TakesAll()).text == "s:['level']"


def test_a_config_field_is_the_wrapper_in_self_and_the_value_in_a_parameter() -> None:
    assert _run(Tuned()).text == "s:3"
    assert _run(TunedUnwrapped()).text == "s:3"


def test_a_name_that_is_neither_a_field_nor_a_parameter_is_not_dropped() -> None:
    run: Any = Summarize().run
    with pytest.raises(TypeError, match="bogus"):
        run(sessions="s", bogus=1)


def test_a_wired_input_the_method_does_not_name_fails_like_before() -> None:
    node = Summarize()(sessions=InputParam("sessions"), extra=InputParam("extra"))
    with pytest.raises(TypeError, match="extra"):
        Pipeline(node).execute(observer=NullObserver(), sessions="s", extra="e")


def test_the_wrapper_keeps_the_signature_of_the_method() -> None:
    assert list(inspect.signature(Summarize.run).parameters) == ["self", "sessions"]


def test_validate_names_a_wired_input_that_run_does_not_accept() -> None:
    node = Summarize()(sessions=InputParam("sessions"), extra=InputParam("extra"))
    with pytest.raises(UnacceptedInputError, match="extra"):
        Pipeline(node).validate()


def test_validate_accepts_a_step_that_leaves_a_field_unnamed() -> None:
    Pipeline(Summarize()(sessions=InputParam("sessions"))).validate()


def test_validate_still_names_a_required_input_that_is_not_bound() -> None:
    with pytest.raises(UnboundInputError, match="sessions"):
        Pipeline(Summarize()()).validate()


def test_validate_checks_arun_too() -> None:
    with pytest.raises(UnboundInputError, match="sessions"):
        Pipeline(SummarizeAsync()()).validate()
    node = SummarizeAsync()(sessions=InputParam("sessions"), extra=InputParam("extra"))
    with pytest.raises(UnacceptedInputError, match="extra"):
        Pipeline(node).validate()


class Names(BaseModel):
    names: list[str]


class Row(BaseModel):
    value: str


class Rows(BaseModel):
    rows: list[Row]


@dataclass(frozen=True)
class Source(Step[Names]):
    def run(self) -> Names:
        return Names(names=["a", "b"])


@dataclass(frozen=True)
class Upper(BatchStep[Rows, Row]):
    """Four hooks, two fields none of them names."""

    suffix: str = "!"

    def items_data(self, source: Names) -> list[str]:
        return source.names

    def process_item(self, item: BatchItem, source: Names) -> Row:
        del source
        return Row(value=item.data.upper() + self.suffix)

    def assemble(self, results: list[Row], source: Names) -> Rows:
        del source
        return Rows(rows=results)


@dataclass(frozen=True)
class UpperItems(BatchStep[Rows, Row]):
    suffix: str = "!"

    def items(self, source: Names) -> list[BatchItem]:
        return [BatchItem(key=f"k_{n}", data=n) for n in source.names]

    def process_item(self, item: BatchItem, source: Names) -> Row:
        del source
        return Row(value=item.data.upper() + self.suffix)

    def assemble(self, results: list[Row], source: Names) -> Rows:
        del source
        return Rows(rows=results)


@dataclass(frozen=True)
class Concurrent(BatchStep[Rows, Row]):
    """A custom ``process_batch`` that forwards every input to ``process_item``."""

    suffix: str = "!"

    def items_data(self, source: Names) -> list[str]:
        return source.names

    def process_item(self, item: BatchItem, source: Names) -> Row:
        del source
        return Row(value=item.data.upper() + self.suffix)

    def process_batch(self, items: list[BatchItem], cache: Any, **inputs: Any) -> None:
        for item in reversed(items):
            cache.save(item.key, self.process_item(item, **inputs))

    def assemble(self, results: list[Row], source: Names) -> Rows:
        del source
        return Rows(rows=results)


def _batch(step: BatchStep[Rows, Row], tmp_path: Path) -> Rows:
    return Pipeline(step(source=Source()()), cache_dir=tmp_path).execute(observer=NullObserver())


def test_the_batch_hooks_may_leave_fields_unnamed(tmp_path: Path) -> None:
    assert [r.value for r in _batch(Upper(suffix="?"), tmp_path).rows] == ["A?", "B?"]


def test_an_items_override_may_leave_fields_unnamed(tmp_path: Path) -> None:
    assert [r.value for r in _batch(UpperItems(suffix="?"), tmp_path).rows] == ["A?", "B?"]


def test_a_custom_process_batch_may_forward_every_input(tmp_path: Path) -> None:
    assert [r.value for r in _batch(Concurrent(suffix="?"), tmp_path).rows] == ["A?", "B?"]


def test_an_unnamed_field_still_changes_the_item_keys() -> None:
    inputs: dict[str, Any] = {"source": Names(names=["a"])}
    first = Upper(suffix="1").items(**inputs, suffix="1")
    second = Upper(suffix="2").items(**inputs, suffix="2")
    assert first[0].key != second[0].key


def test_item_keys_are_the_same_as_before_the_hooks_were_wrapped() -> None:
    # Pinned at KEY_FORMAT 3. Shout is in tests/golden_batch_steps.py.
    keys = [i.key for i in Shout(tag="x").items(names=["a", "b"], tag="x")]
    assert keys == ["59518923c2a20732", "a0f9babd26834e75"]
    other = [i.key for i in Shout(tag="y").items(names=["a", "b"], tag="y")]
    assert other == ["319758ead2b498a1", "1571834f6014fe5f"]


def test_validate_checks_the_batch_hooks(tmp_path: Path) -> None:
    node = Upper()(source=Source()(), extra=InputParam("extra"))
    with pytest.raises(UnacceptedInputError, match="extra"):
        Pipeline(node, cache_dir=tmp_path).validate()
    Pipeline(Upper()(source=Source()()), cache_dir=tmp_path).validate()
