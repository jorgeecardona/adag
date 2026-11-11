"""Cache a result that is not one JSON document: JSONL here."""

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from adag import InputParam, Pipeline, Step


@dataclass(frozen=True)
class Records(Step[list[dict[str, int]]]):
    def run(self, n: int) -> list[dict[str, int]]:
        print("computing records")
        return [{"i": i} for i in range(n)]

    def save_result(self, result: list[dict[str, int]], path: Path) -> None:
        path.write_text("".join(json.dumps(r) + "\n" for r in result))

    def load_result(self, path: Path) -> list[dict[str, int]]:
        return [json.loads(line) for line in path.read_text().splitlines()]


with tempfile.TemporaryDirectory() as cache:
    pipeline = Pipeline(Records()(n=InputParam[int]("n")), cache_dir=Path(cache))
    print(pipeline.execute(n=3))
    print(pipeline.execute(n=3))
