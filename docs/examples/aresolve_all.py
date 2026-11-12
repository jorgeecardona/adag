import asyncio
from dataclasses import dataclass

from pydantic import BaseModel

from adag import AsyncStep, NullObserver, ResolveContext, aresolve_all


class Page(BaseModel):
    url: str
    size: int


@dataclass(frozen=True)
class Fetch(AsyncStep[Page]):
    url: str

    async def arun(self, url: str) -> Page:
        await asyncio.sleep(0.01)  # stands in for an HTTP call
        return Page(url=url, size=len(url))


async def handler() -> None:
    targets = [Fetch(url=u)() for u in ["a.org", "bb.org", "a.org"]]
    context = ResolveContext(observer=NullObserver())
    pages = await aresolve_all(targets, context, max_concurrency=2)
    print([p.size for p in pages])


asyncio.run(handler())
