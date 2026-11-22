"""A batch step whose hooks name every field. Its item keys are pinned in test_unnamed_fields."""

from dataclasses import dataclass

from pydantic import BaseModel

from adag import BatchItem, BatchStep


class Piece(BaseModel):
    text: str


class Pieces(BaseModel):
    items: list[Piece]


@dataclass(frozen=True)
class Shout(BatchStep[Pieces, Piece]):
    items_source_keys = ("names",)
    tag: str = "x"

    def items_data(self, names: list[str], tag: str) -> list[str]:
        return names

    def process_item(self, item: BatchItem, names: list[str], tag: str) -> Piece:
        return Piece(text=item.data.upper() + tag)

    def assemble(self, results: list[Piece], names: list[str], tag: str) -> Pieces:
        return Pieces(items=results)
