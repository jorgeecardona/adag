"""FanOutStep: one sub-graph per key, run on a thread pool, results as a dict."""

from dataclasses import dataclass

from pydantic import BaseModel

from adag import FanOutStep, NullObserver, Pipeline, Step


class Size(BaseModel):
    chars: int


@dataclass(frozen=True)
class Measure(Step[Size]):
    text: str

    def run(self, text: str) -> Size:
        return Size(chars=len(text))


documents = {"intro": "Hello.", "body": "A longer paragraph.", "end": "Bye."}
sizes = FanOutStep(
    over=documents,
    factory=lambda key, text: Measure(text=text)(),
    max_workers=3,
)()

result = Pipeline(sizes).execute(observer=NullObserver())
print({key: size.chars for key, size in result.items()})
