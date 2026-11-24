"""Golden keys of a static fan-out: the keys of ``FanOutStep(over=dict, factory=...)`` must not change.

A change to a value here changes the downstream keys of every static fan-out a user has on disk.
"""

from dataclasses import dataclass

from pydantic import BaseModel

from adag import FanOutStep, Step, StepResult


class Size(BaseModel):
    chars: int


@dataclass(frozen=True)
class Measure(Step[Size]):
    text: str

    def run(self, text: str) -> Size:
        return Size(chars=len(text))


def _factory(key: str, text: str) -> StepResult[Size]:
    return Measure(text=text)()


def _fan_out() -> FanOutStep[str, Size]:
    return FanOutStep(over={"intro": "Hello.", "body": "A longer paragraph."}, factory=_factory)


def test_static_fan_out_step_hash_is_pinned() -> None:
    assert _fan_out().content_hash() == "60d6482fbe31"


def test_static_fan_out_node_key_is_pinned() -> None:
    assert _fan_out()().content_hash() == "5df6d0c9b8a0"


def test_static_fan_out_branch_keys_are_pinned() -> None:
    assert _factory("intro", "Hello.").content_hash() == "a75b42c70943"
    assert _factory("body", "A longer paragraph.").content_hash() == "130341c95bab"
