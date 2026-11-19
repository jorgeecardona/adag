"""FanOutStep: one sub-graph per key, results as a dict."""

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
)()

result = Pipeline(sizes).execute(observer=NullObserver())
print({key: size.chars for key, size in result.items()})
