"""Worker events: what a dispatched branch tells the main process while it runs.

This module is internal. The events and ``BranchPolicy.silent_timeout_seconds`` are the public
side (see ``docs/dispatch.md``).

The worker (``adag resolve-branch``) writes one small record to the shared store under
``.events/<branch hash>``. It holds the last ``MAX_EVENTS`` events and a counter that grows with
every write. ``RemoteDispatcher`` reads the record once per poll, forwards each new event to the
main observer as ``branch.<event>``, and reads the counter as a sign of life.

No clock crosses the wire. The main process notices that the counter changed, and uses its own
clock to say how long ago that was. The worker writes at most every ``MIN_WRITE_GAP`` seconds, and
at least every ``HEARTBEAT_SECONDS`` seconds while it is alive. One thread does all the writing, so
a slow store never stalls a step.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self, cast

import structlog

from adag.observers import LoggingObserver

if TYPE_CHECKING:
    from types import TracebackType

    from adag.cache_store import CacheStore

logger = structlog.get_logger()

EVENTS_PREFIX = ".events/"
"""Store key prefix of the worker records: ``.events/<branch hash>``."""

HEARTBEAT_SECONDS = 10.0
"""A live worker writes its record at least this often. Set ``silent_timeout_seconds`` to more."""

MAX_EVENTS = 200
"""A record keeps the newest events up to this number. Older ones are dropped."""

MIN_WRITE_GAP = 2.0
"""Events that arrive faster than this are written together."""

JOIN_SECONDS = 10.0
"""How long the end of a branch waits for the writer thread before it gives up on it."""

ERROR_CHARS = 2000
"""How much of an exception message goes into a ``failed`` event."""


def events_key(branch_hash: str) -> str:
    """The store key of the record of one branch."""
    return f"{EVENTS_PREFIX}{branch_hash}"


@dataclass(frozen=True)
class WorkerEvent:
    """One event of a worker: ``seq`` counts up from 1, ``fields`` is JSON data."""

    seq: int
    event: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class WorkerRecord:
    """What a worker wrote: ``token`` names the worker process, ``beat`` counts its writes."""

    token: str
    beat: int
    events: tuple[WorkerEvent, ...]

    def to_json(self) -> str:
        """The record as one JSON text."""
        return json.dumps(
            {
                "token": self.token,
                "beat": self.beat,
                "events": [
                    {"seq": e.seq, "event": e.event, "fields": e.fields} for e in self.events
                ],
            },
            default=str,
        )

    @classmethod
    def from_json(cls, text: str) -> WorkerRecord | None:
        """Read a record. Return ``None`` for text that is not one, so a bad write is ignored."""
        try:
            data = cast("dict[str, Any]", json.loads(text))
            return cls(
                token=str(data["token"]),
                beat=int(data["beat"]),
                events=tuple(
                    WorkerEvent(int(e["seq"]), str(e["event"]), dict(e["fields"]))
                    for e in data["events"]
                ),
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return None


class WorkerEventObserver(LoggingObserver):
    """Observer of a worker: logs like ``LoggingObserver`` and writes each event to the store.

    Use it as a context manager. A thread keeps the record alive and writes new events. When the
    block ends with an error, the last events are written and the record stays. When the block
    ends cleanly, the result is in the cache and the record is deleted. A store that fails is
    logged and skipped: events never fail a branch, and a callback never waits for the store.
    """

    def __init__(
        self,
        store: CacheStore,
        branch_hash: str,
        *,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        gap_seconds: float = MIN_WRITE_GAP,
    ) -> None:
        self._store = store
        self._key = events_key(branch_hash)
        self._token = uuid.uuid4().hex
        self._heartbeat = heartbeat_seconds
        self._gap = gap_seconds
        self._lock = threading.Lock()
        self._events: list[WorkerEvent] = []
        self._seq = 0
        self._beat = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        self._write()
        self._thread = threading.Thread(target=self._keep_alive, name="adag-worker-events")
        self._thread.daemon = True
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(JOIN_SECONDS)
        if exc_type is None:
            self._delete()
        else:
            self._write()

    def on_step_start(self, step: str) -> None:
        super().on_step_start(step)
        self._add("step_start", {"step": step})

    def on_step_cached(self, step: str) -> None:
        super().on_step_cached(step)
        self._add("step_cached", {"step": step})

    def on_step_finished(self, step: str, seconds: float) -> None:
        super().on_step_finished(step, seconds)
        self._add("step_finished", {"step": step, "seconds": round(seconds, 4)})

    def on_event(self, event: str, fields: dict[str, Any]) -> None:
        super().on_event(event, fields)
        self._add("event", {"name": event, "fields": fields})

    def failed(self, error: BaseException) -> None:
        """Record that the branch raised ``error``. The end of the block writes it."""
        self._add("failed", {"error_type": type(error).__name__, "error": str(error)[:ERROR_CHARS]})

    def _add(self, event: str, fields: dict[str, Any]) -> None:
        try:
            safe: dict[str, Any] = json.loads(json.dumps(fields, default=str))
        except (TypeError, ValueError):  # a key that is not text, or a loop in the data
            safe = {"repr": repr(fields)[:ERROR_CHARS]}
        with self._lock:
            self._seq += 1
            self._events.append(WorkerEvent(self._seq, event, safe))
            del self._events[:-MAX_EVENTS]
        self._wake.set()

    def _keep_alive(self) -> None:
        """Write when an event arrives, and at least once per heartbeat. Retry a failed write soon."""
        wait = self._heartbeat
        while not self._stop.is_set():
            self._wake.wait(wait)
            if self._stop.is_set():
                break
            self._wake.clear()
            self._stop.wait(self._gap)  # let a burst of events gather into one write
            wait = self._heartbeat if self._write() else self._gap

    def _delete(self) -> None:
        try:
            self._store.delete(self._key)
        except Exception as error:
            logger.warning(
                "worker.events_delete_failed", error_type=type(error).__name__, error=str(error)
            )

    def _write(self) -> bool:
        """Write the record. Return False when the store failed."""
        with self._lock:
            self._beat += 1
            record = WorkerRecord(self._token, self._beat, tuple(self._events))
        try:
            self._store.set(self._key, record.to_json())
        except Exception as error:
            logger.warning(
                "worker.events_write_failed", error_type=type(error).__name__, error=str(error)
            )
            return False
        return True
