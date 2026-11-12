"""``Pipeline.metadata`` is typed as ``JsonValue``, checked by pyright in strict mode."""

import json
import subprocess
import sys
from pathlib import Path

SNIPPET = """
from dataclasses import dataclass

from pydantic import BaseModel

from adag import JsonValue, Pipeline, Step


class Num(BaseModel):
    value: int


@dataclass(frozen=True)
class One(Step[Num]):
    def run(self) -> Num:
        return Num(value=1)


counts: dict[str, int] = {"a": 1}
pipeline = Pipeline(One()(), metadata=counts)
loaded: JsonValue = pipeline.metadata
if isinstance(loaded, dict):
    print(loaded["a"])
print(pipeline.metadata["a"])
"""


def test_metadata_accepts_a_dict_and_must_be_narrowed(tmp_path: Path) -> None:
    source = tmp_path / "snippet.py"
    source.write_text(SNIPPET)
    (tmp_path / "pyrightconfig.json").write_text(
        json.dumps(
            {
                "typeCheckingMode": "strict",
                "pythonVersion": "3.12",
                "reportIncompatibleMethodOverride": False,
            }
        )
    )
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson", str(source)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        timeout=300,
    )
    report = json.loads(result.stdout)
    errors = [d for d in report["generalDiagnostics"] if d["severity"] == "error"]
    lines = sorted({d["range"]["start"]["line"] + 1 for d in errors})
    unnarrowed = SNIPPET.splitlines().index('print(pipeline.metadata["a"])') + 1
    assert lines == [unnarrowed], errors
