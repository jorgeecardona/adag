"""Where the calls of a cache store run on the async path.

A store says so with its ``runs_on`` attribute. ``"loop"``, also the value of a store without the
attribute, calls it on the event loop. ``"thread"`` calls it in a thread of adag's own pool.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

_lock = threading.Lock()
# Apart from the loop's default pool: a store call must not wait behind long step bodies.
_pools: dict[str, ThreadPoolExecutor] = {}


def in_thread(store: object) -> bool:
    """Whether ``store`` asks for its calls to run off the event loop.

    Raises:
        ValueError: ``runs_on`` is neither ``"loop"`` nor ``"thread"``.
    """
    place = getattr(store, "runs_on", "loop")
    if place not in ("loop", "thread"):
        raise ValueError(f"{type(store).__name__}.runs_on is {place!r}; use 'loop' or 'thread'.")
    return place == "thread"


def submit[R](call: Callable[[], R]) -> asyncio.Future[R]:
    """Start ``call`` in a store thread, under a copy of the current context variables."""
    context = contextvars.copy_context()
    return asyncio.get_running_loop().run_in_executor(_pool(), context.run, call)


def _pool() -> ThreadPoolExecutor:
    with _lock:
        pool = _pools.get("store")
        if pool is None:
            pool = ThreadPoolExecutor(
                max_workers=min(32, (os.cpu_count() or 1) + 4), thread_name_prefix="adag-store"
            )
            _pools["store"] = pool
        return pool


def _forget_pool() -> None:
    """Drop the pool in a forked child, which has none of its threads."""
    global _lock  # noqa: PLW0603  (the parent may have held the lock when it forked)
    _lock = threading.Lock()
    _pools.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_pool)
