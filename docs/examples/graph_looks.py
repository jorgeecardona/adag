"""Graph looks: draw one pipeline with each style, input mode and density."""

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from adag import InputParam, NullObserver, Pipeline, Step, dag
from adag.visualization import SvgOptions


class Text(BaseModel):
    value: str


@dataclass(frozen=True)
class Load(Step[Text]):
    """Read the CSV file."""

    def run(self, path: str) -> Text:
        return Text(value=f"  rows of {path}  ")


@dataclass(frozen=True)
class Clean(Step[Text]):
    """Normalize the text."""

    def run(self, raw: Text, case: str) -> Text:
        text = raw.value.strip()
        return Text(value=text.lower() if case == "lower" else text.upper())


@dataclass(frozen=True)
class Count(Step[Text]):
    """Count the characters."""

    def run(self, raw: Text) -> Text:
        return Text(value=str(len(raw.value)))


@dataclass(frozen=True)
class Report(Step[Text]):
    """Write the summary."""

    def run(self, cleaned: Text, size: Text) -> Text:
        return Text(value=f"{cleaned.value} ({size.value} chars)")


def build(case: str):
    path = InputParam.with_default("path", "data.csv")
    raw = Load()(path=path)
    cleaned = Clean()(raw=raw, case=InputParam.with_default("case", case))
    return Report()(cleaned=cleaned, size=Count()(raw=raw))


cache = Path(tempfile.mkdtemp())
Pipeline(build("lower"), cache_dir=cache).execute(observer=NullObserver())

# Change one input. Load and Count stay cached; Clean and Report are pending.
target = build("upper")
looks = {
    "look_blueprint": SvgOptions(),
    "look_band": SvgOptions(style="band"),
    "look_classic": SvgOptions(style="classic"),
    "inputs_tabs": SvgOptions(inputs="tabs"),
    "inputs_rows": SvgOptions(inputs="rows"),
    "density_compact": SvgOptions(density="compact"),
    "theme_dark": SvgOptions(theme="dark", legend=True),
}
out = Path(sys.argv[1]) if len(sys.argv) > 1 else cache
if out.suffix == ".svg":  # the example test passes one file path: use its folder
    out = out.parent
for name, options in looks.items():
    dag(target, cache_dir=cache, path=out / f"{name}.svg", options=options)
print(f"wrote {len(looks)} graphs")
