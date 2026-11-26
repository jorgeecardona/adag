"""``runs_on`` on a cache store: where its calls run on the async path."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, create_mock_engine
from structlog.testing import capture_logs

from adag import InputParam, Pipeline, Step, aresolve_all
from adag._store_io import in_thread
from adag.cache_store import (
    CacheStore,
    FileSystemCacheStore,
    MemoryCacheStore,
    RedisCacheStore,
    SqlCacheStore,
    SqliteCacheStore,
)
from adag.deferred import ResolveContext
from adag.observers import LoggingObserver, RunTreeObserver
from adag.step import StepResult

n = InputParam[int]("n")
RUNS: list[int] = []


class Num(BaseModel):
    n: int


@dataclass(frozen=True)
class Double(Step[Num]):
    def run(self, n: int) -> Num:
        RUNS.append(n)
        return Num(n=n * 2)


@dataclass(frozen=True)
class Add(Step[Num]):
    def run(self, n: Num) -> Num:
        return Num(n=n.n + 1)


class Recording(MemoryCacheStore):
    """Records each call and the thread it ran on."""

    def __init__(self, place: str = "thread") -> None:
        super().__init__()
        self.runs_on = cast("Any", place)
        self.calls: list[tuple[str, str]] = []
        self.threads: set[int] = set()

    def _note(self, method: str, key: str) -> None:
        self.calls.append((method, key))
        self.threads.add(threading.get_ident())

    def get(self, key: str) -> str | None:
        self._note("get", key)
        return super().get(key)

    def set(self, key: str, value: str) -> None:
        self._note("set", key)
        super().set(key, value)

    def exists(self, key: str) -> bool:
        self._note("exists", key)
        return super().exists(key)

    def stored(self) -> dict[str, str]:
        """The contents without the timestamps."""
        contents: dict[str, str] = {}
        for key in self.iter_keys():
            text = cast("str", MemoryCacheStore.get(self, key))
            if key.endswith(".done"):
                text = ""
            elif key.startswith(".keys/"):
                text = json.dumps({**json.loads(text), "saved_at": ""}, sort_keys=True)
            contents[key] = text
        return contents


class Events(LoggingObserver):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []
        self.threads: set[int] = set()

    def _note(self, event: str) -> None:
        self.seen.append(event)
        self.threads.add(threading.get_ident())

    def on_step_start(self, step: str) -> None:
        self._note(f"start {step}")

    def on_step_cached(self, step: str) -> None:
        self._note(f"cached {step}")

    def on_step_finished(self, step: str, seconds: float) -> None:
        self._note(f"finished {step}")

    def on_event(self, event: str, fields: dict[str, Any]) -> None:
        self._note(event)


def _graph() -> Any:
    return Add()(n=Double()(n=n))


def _roots(count: int) -> list[Any]:
    return [Double()(n=InputParam[int](f"n{i}")) for i in range(count)]


def _params(count: int) -> dict[str, int]:
    return {f"n{i}": i for i in range(count)}


class Plain:
    """A third-party store written before ``runs_on``."""

    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str) -> None: ...

    def exists(self, key: str) -> bool:
        return False

    def delete(self, key: str) -> None: ...

    def iter_keys(self, prefix: str = "") -> Iterable[str]:
        return []


def _mock_engine(url: str) -> Engine:
    def run(sql: Any, *args: Any, **kwargs: Any) -> None: ...

    return cast("Engine", create_mock_engine(url, run))


def test_local_stores_run_on_the_loop(tmp_path: Path) -> None:
    stores: list[CacheStore] = [
        FileSystemCacheStore(tmp_path),
        MemoryCacheStore(),
        SqliteCacheStore(tmp_path / "cache.db"),
        SqlCacheStore(url=f"sqlite:///{tmp_path / 'sql.db'}"),
        SqlCacheStore(url="sqlite://"),
    ]

    assert [in_thread(store) for store in stores] == [False] * len(stores)


def test_network_stores_run_in_a_thread() -> None:
    assert in_thread(RedisCacheStore(client=cast("Any", object())))
    assert in_thread(SqlCacheStore(engine=_mock_engine("postgresql://")))


def test_a_store_without_the_attribute_runs_on_the_loop_and_is_still_a_cache_store() -> None:
    assert not in_thread(Plain())
    assert isinstance(Plain(), CacheStore)


def test_an_instance_can_choose_the_loop() -> None:
    store = RedisCacheStore(client=cast("Any", object()))
    store.runs_on = "loop"

    assert not in_thread(store)


def test_a_subclass_choice_wins(tmp_path: Path) -> None:
    class Remote(FileSystemCacheStore):
        runs_on = "thread"

    class Near(SqlCacheStore):
        runs_on = "loop"

    assert in_thread(Remote(tmp_path))
    assert not in_thread(Near(engine=_mock_engine("postgresql://")))


def test_an_unknown_value_is_refused() -> None:
    store = MemoryCacheStore()
    store.runs_on = cast("Any", "process")

    with pytest.raises(ValueError, match="MemoryCacheStore.runs_on is 'process'"):
        in_thread(store)


# --- the async path ---------------------------------------------------------------------------


def _through_aresolve_all(node: Any, context: ResolveContext) -> Any:
    async def main() -> Any:
        return (await aresolve_all([node], context))[0]

    return main()


def _through_aresolve(node: Any, context: ResolveContext) -> Any:
    return node.aresolve(context)


@pytest.mark.parametrize("resolve", [_through_aresolve_all, _through_aresolve])
@pytest.mark.parametrize("place", ["thread", "loop"])
def test_store_calls_run_where_the_store_says(
    resolve: Callable[[Any, ResolveContext], Any], place: str
) -> None:
    store = Recording(place)

    async def main() -> int:
        for _ in range(2):  # a miss, then a hit
            context = ResolveContext(store=store, params={"n": 3})
            assert await resolve(_graph(), context) == Num(n=7)
        return threading.get_ident()

    loop_thread = asyncio.run(main())

    assert store.calls
    if place == "thread":
        assert loop_thread not in store.threads
    else:
        assert store.threads == {loop_thread}


def test_both_paths_make_the_same_calls_and_write_the_same_entries() -> None:
    sync, threaded = Recording("loop"), Recording("thread")

    for _ in range(2):
        Pipeline(output=_graph(), store=sync).execute(n=3)
    for _ in range(2):
        asyncio.run(Pipeline(output=_graph(), store=threaded).aexecute(n=3))

    # ``aresolve_all`` also asks ``exists`` while it plans the levels, on either kind of store.
    on_the_loop = Recording("loop")
    for _ in range(2):
        asyncio.run(Pipeline(output=_graph(), store=on_the_loop).aexecute(n=3))
    assert threaded.calls == on_the_loop.calls
    assert threaded.stored() == sync.stored()


def test_the_loop_runs_while_a_store_call_is_in_flight() -> None:
    gate = threading.Event()

    class Gated(Recording):
        def exists(self, key: str) -> bool:
            assert gate.wait(5), "the store call blocked the loop that opens the gate"
            return super().exists(key)

    async def main() -> Num:
        asyncio.get_running_loop().call_soon(gate.set)
        return await Double()(n=n).aresolve(ResolveContext(store=Gated(), params={"n": 1}))

    assert asyncio.run(main()) == Num(n=2)


def test_the_reads_of_gathered_nodes_overlap() -> None:
    together = threading.Barrier(2, timeout=5)

    class Meeting(Recording):
        def get(self, key: str) -> str | None:
            if not key.startswith(".keys/"):
                together.wait()
            return super().get(key)

    async def main() -> list[Any]:
        context = ResolveContext(store=Meeting(), params=_params(2))
        return await aresolve_all(_roots(2), context)

    assert asyncio.run(main()) == [Num(n=0), Num(n=2)]


def test_callbacks_stay_on_the_loop_and_keep_their_order() -> None:
    store, cold, warm = Recording(), Events(), Events()
    progress: list[int] = []

    async def main() -> int:
        for observer in (cold, warm):
            context = ResolveContext(
                store=store,
                params={"n": 3},
                observer=observer,
                on_progress=lambda _: progress.append(threading.get_ident()),
            )
            await aresolve_all([Double()(n=n)], context)
        return threading.get_ident()

    loop_thread = asyncio.run(main())

    assert cold.seen == ["start Double", "finished Double", "step.saved"]
    assert warm.seen == ["start Double", "cached Double"]
    assert cold.threads == warm.threads == set(progress) == {loop_thread}


def test_a_gathered_level_of_hits_does_not_nest_in_the_run_tree() -> None:
    store = Recording()
    tree = RunTreeObserver()

    async def main() -> None:
        await aresolve_all(_roots(4), ResolveContext(store=store, params=_params(4)))
        context = ResolveContext(store=store, params=_params(4), observer=tree)
        await aresolve_all(_roots(4), context)

    asyncio.run(main())

    assert tree.render_tree().splitlines() == ["Double  ⟳ cached"] * 4


@pytest.mark.parametrize("method", ["exists", "set"])
def test_a_store_failure_reaches_the_caller_and_frees_the_node(method: str) -> None:
    class Broken(Recording):
        def exists(self, key: str) -> bool:
            if method == "exists":
                raise OSError("store down")
            return super().exists(key)

        def set(self, key: str, value: str) -> None:
            if method == "set":
                raise OSError("store down")
            super().set(key, value)

    context = ResolveContext(store=Broken(), params={"n": 1})

    async def main() -> None:
        with pytest.raises(OSError, match="store down"):
            await Double()(n=n).aresolve(context)

    asyncio.run(main())
    assert context._ainflight == {}  # pyright: ignore[reportPrivateUsage]


def test_a_cancel_during_a_write_keeps_the_claim_until_the_write_ends() -> None:
    writing, release = threading.Event(), threading.Event()

    class Slow(Recording):
        def set(self, key: str, value: str) -> None:
            if not writing.is_set():
                writing.set()
                assert release.wait(5)
            super().set(key, value)

    store = Slow()
    context = ResolveContext(store=store, params={"n": 5})
    RUNS.clear()

    async def main() -> None:
        node = Double()(n=n)
        first = asyncio.create_task(node.aresolve(context))
        assert await asyncio.to_thread(writing.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert context._ainflight  # pyright: ignore[reportPrivateUsage]
        second = asyncio.create_task(Double()(n=n).aresolve(context))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not second.done()
        release.set()
        assert await asyncio.wait_for(second, 5) == Num(n=10)

    asyncio.run(main())

    assert RUNS == [5]
    key = Double()(n=n).content_hash(params={"n": 5})
    assert key is not None
    assert {key, f"{key}.done", f".keys/{key}.json"} <= set(store.stored())
    assert context._ainflight == {}  # pyright: ignore[reportPrivateUsage]


def test_a_cancel_during_a_read_frees_the_node_at_once() -> None:
    reading, release = threading.Event(), threading.Event()

    class Slow(Recording):
        def exists(self, key: str) -> bool:
            reading.set()
            assert release.wait(5)
            return super().exists(key)

    context = ResolveContext(store=Slow(), params={"n": 1})

    async def main() -> None:
        task = asyncio.create_task(Double()(n=n).aresolve(context))
        assert await asyncio.to_thread(reading.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert context._ainflight == {}  # pyright: ignore[reportPrivateUsage]
        release.set()

    asyncio.run(main())


def test_sqlite_through_sqlalchemy_works_on_the_async_path() -> None:
    pipe = Pipeline(output=Double()(n=n), store=SqlCacheStore(url="sqlite://"))

    assert asyncio.run(pipe.aexecute(n=4)) == Num(n=8)


class _FirstSetWaits(Recording):
    """The first ``set`` waits for ``release``, and then fails when ``fail`` is set."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.writing, self.release = threading.Event(), threading.Event()
        self.fail = fail

    def set(self, key: str, value: str) -> None:
        if not self.writing.is_set():
            self.writing.set()
            assert self.release.wait(5)
            if self.fail:
                raise OSError("store down")
        super().set(key, value)


async def _cancel_in_the_write(store: _FirstSetWaits, context: ResolveContext) -> None:
    task = asyncio.create_task(Double()(n=n).aresolve(context))
    assert await asyncio.to_thread(store.writing.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("closed", [True, False], ids=["closed", "stopped"])
def test_a_claim_left_on_another_loop_does_not_block_a_later_run(closed: bool) -> None:
    store = _FirstSetWaits()
    context = ResolveContext(store=store, params={"n": 6})
    first = asyncio.new_event_loop()
    try:
        first.run_until_complete(_cancel_in_the_write(store, context))
        assert context._ainflight  # pyright: ignore[reportPrivateUsage]
        if closed:
            first.close()

        async def again() -> Num:
            return await asyncio.wait_for(Double()(n=n).aresolve(context), 5)

        assert asyncio.run(again()) == Num(n=12)
    finally:
        store.release.set()
        first.close()
    assert context._ainflight == {}  # pyright: ignore[reportPrivateUsage]


def test_a_write_that_fails_after_a_cancel_is_logged_and_frees_the_node() -> None:
    store = _FirstSetWaits(fail=True)
    context = ResolveContext(store=store, params={"n": 7})

    async def main() -> None:
        await _cancel_in_the_write(store, context)
        (claim,) = context._ainflight.values()  # pyright: ignore[reportPrivateUsage]
        store.release.set()
        await asyncio.wait_for(claim, 5)
        assert context._ainflight == {}  # pyright: ignore[reportPrivateUsage]

    with capture_logs() as logs:
        asyncio.run(main())

    failed = [log for log in logs if log["event"] == "step.write_after_cancel_failed"]
    assert [log["error"] for log in failed] == ["store down"]


def test_keys_are_computed_on_the_loop_when_two_runs_share_a_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads: set[int] = set()
    compute = cast(
        "Callable[[StepResult[Any], dict[str, Any] | None], str | None]",
        vars(StepResult)["_compute_content_hash"],
    )

    def spy(node: StepResult[Any], params: dict[str, Any] | None = None) -> str | None:
        threads.add(threading.get_ident())
        return compute(node, params)

    monkeypatch.setattr(StepResult, "_compute_content_hash", spy)
    pipe = Pipeline(output=_graph(), store=Recording())

    async def main() -> int:
        results = await asyncio.gather(pipe.aexecute(n=1), pipe.aexecute(n=2))
        assert results == [Num(n=3), Num(n=5)]
        return threading.get_ident()

    loop_thread = asyncio.run(main())

    assert threads == {loop_thread}


@dataclass(frozen=True)
class Page(Step[Num]):
    """Caches to a path, so only its key record goes to the store."""

    def run(self, n: int) -> Num:
        return Num(n=n)

    def save_result(self, result: Num, path: Path) -> None:
        path.write_text(str(result.n))

    def load_result(self, path: Path) -> Num:
        return Num(n=int(path.read_text()))


def test_a_path_cached_step_sends_its_key_record_through_the_thread(tmp_path: Path) -> None:
    store = Recording()

    async def main() -> int:
        pipe = Pipeline(output=Page()(n=n), store=store, cache_dir=tmp_path)
        assert await pipe.aexecute(n=8) == Num(n=8)
        assert await pipe.aexecute(n=8) == Num(n=8)
        return threading.get_ident()

    loop_thread = asyncio.run(main())

    assert [method for method, key in store.calls if key.startswith(".keys/")] == ["set"]
    assert loop_thread not in store.threads
