"""ItemCache on a different backend: SQLite instead of one file per item."""

import tempfile
from pathlib import Path

from pydantic import BaseModel

from adag import ItemCache
from adag.cache_store import SqliteCacheStore


class Answer(BaseModel):
    text: str


folder = Path(tempfile.mkdtemp())
store = SqliteCacheStore(folder / "items.db")
cache = ItemCache[Answer](folder, "answers", Answer, store=store)

keys = [cache.key(question) for question in ["q1", "q2", "q3"]]
cache.save(keys[0], Answer(text="first"))

cached, missing = cache.partition(keys)
print("cached:", len(cached), "missing:", len(missing))
print(cache.load(keys[0]))
