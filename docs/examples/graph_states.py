"""Graph states: after one field changes, only that step and its downstream steps are pending."""

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import NullObserver, Pipeline, Step, dag


class Text(BaseModel):
    value: str


@dataclass(frozen=True)
class Load(Step[Text]):
    def run(self) -> Text:
        return Text(value="  Hello, DAG  ")


@dataclass(frozen=True)
class Clean(Step[Text]):
    case: str = "lower"

    def run(self, raw: Text, case: str) -> Text:
        text = raw.value.strip()
        return Text(value=text.lower() if case == "lower" else text.upper())


@dataclass(frozen=True)
class Count(Step[Text]):
    def run(self, raw: Text) -> Text:
        return Text(value=str(len(raw.value)))


@dataclass(frozen=True)
class Report(Step[Text]):
    def run(self, cleaned: Text, size: Text) -> Text:
        return Text(value=f"{cleaned.value} ({size.value} chars)")


def build(case: str):
    raw = Load()()
    return Report()(cleaned=Clean(case=case)(raw=raw), size=Count()(raw=raw))


cache = Path(tempfile.mkdtemp())
Pipeline(build("lower"), cache_dir=cache).execute(observer=NullObserver())

# Change one field of Clean. Load and Count stay cached; Clean and Report are pending.
out = Path(sys.argv[1]) if len(sys.argv) > 1 else cache / "graph_states.svg"
dag(build("upper"), cache_dir=cache, path=out)
print(f"wrote {out.name}")
