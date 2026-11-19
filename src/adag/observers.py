"""Concrete PipelineObserver implementations.

The protocol and the no-op NullObserver live in adag.deferred. Observers that do something
(log the lifecycle, render the run as a tree) live here.

LoggingObserver is the one Pipeline.execute installs by default. RunTreeObserver builds an
indented tree of the run from the lifecycle events: StepResult.resolve calls on_step_start
before it resolves a step's inputs and on_step_finished after the step runs, so every
child's start and finish fall inside its parent's. A per-thread stack tracks the current
parent, and a cache hit (on_step_cached) closes a step with no children. Under
``Pipeline.execute(parallel=True)`` each worker thread brackets its own steps, and roots merge
under a lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import structlog

from adag.deferred import NullObserver

logger = structlog.get_logger()


class LoggingObserver(NullObserver):
    """The default observer (installed by ``Pipeline.execute``): logs each step's
    lifecycle as a structlog event.

    This is the single place per-step logging happens. ``resolve`` emits the
    events and this turns them into log lines. To make one event louder or quieter,
    change its level here (one place). For full silence, pass ``NullObserver``.
    """

    def on_step_start(self, step: str) -> None:
        logger.info("step.start", step=step)

    def on_step_cached(self, step: str) -> None:
        logger.info("step.cache_hit", step=step)

    def on_step_finished(self, step: str, seconds: float) -> None:
        logger.info("step.finished", step=step, seconds=round(seconds, 4))

    def on_event(self, event: str, fields: dict[str, Any]) -> None:
        logger.info(event, **fields)


def _empty_children() -> list[RunNode]:
    """Typed default for ``RunNode.children``: pyright can't infer ``list[RunNode]`` from bare ``list``."""
    return []


@dataclass
class RunNode:
    """One step in the reconstructed execution tree."""

    name: str
    cached: bool = False
    seconds: float | None = None
    children: list[RunNode] = field(default_factory=_empty_children)


class _ThreadRunState(threading.local):
    """Per-thread tree-building state. ``threading.local`` re-runs ``__init__`` for each
    thread that touches it, so every worker thread under parallel execution starts with its
    own empty stack and no pending label, a shared stack would interleave concurrent steps.
    """

    def __init__(self) -> None:
        self.stack: list[RunNode] = []
        self.pending_label: str | None = None


class RunTreeObserver(NullObserver):
    """Builds an indented tree of a single pipeline run, annotated with timing and cache-hit markers.

    Render it with ``render_tree()`` after ``execute()``. Use one observer per ``execute()``: it
    is not reusable across runs. It is safe under threads: per-thread stacks keep each worker's
    bracketing consistent, and roots merge under a lock. Under ``parallel=True`` the tree is flat,
    because level-scheduled execution has no recursion to nest.
    """

    def __init__(self) -> None:
        self._roots: list[RunNode] = []
        self._roots_lock = threading.Lock()
        self._state = _ThreadRunState()

    def on_fan_out_branch(self, key: str) -> None:
        # The next step to start is the root of this fan-out branch; label it by key.
        self._state.pending_label = key

    def on_loop_round(self, number: int, key: str) -> None:
        # The next step to start is the root of this loop round; label it by round.
        self._state.pending_label = f"round {number}"

    def on_step_start(self, step: str) -> None:
        label = self._state.pending_label
        name = step if label is None else f"{step} [{label}]"
        self._state.pending_label = None
        node = RunNode(name=name)
        stack = self._state.stack
        if stack:
            stack[-1].children.append(node)
        else:
            with self._roots_lock:
                self._roots.append(node)
        stack.append(node)

    def on_step_cached(self, step: str) -> None:
        node = self._state.stack.pop()
        node.cached = True

    def on_step_finished(self, step: str, seconds: float) -> None:
        node = self._state.stack.pop()
        node.seconds = seconds

    def render_tree(self) -> str:
        """Render the collected run as an indented tree string."""
        lines: list[str] = []
        for root in self._roots:
            self._render(root, prefix="", connector="", lines=lines)
        return "\n".join(lines)

    def _render(self, node: RunNode, prefix: str, connector: str, lines: list[str]) -> None:
        lines.append(f"{prefix}{connector}{node.name}  {self._marker(node)}")
        child_prefix = (
            prefix if not connector else prefix + ("   " if connector == "└─ " else "│  ")
        )
        for index, child in enumerate(node.children):
            is_last = index == len(node.children) - 1
            self._render(child, child_prefix, "└─ " if is_last else "├─ ", lines)

    def _marker(self, node: RunNode) -> str:
        if node.cached:
            return "⟳ cached"
        if node.seconds is None:
            return "● (unfinished)"
        if node.seconds < 1.0:
            return f"● {node.seconds * 1000:.0f}ms"
        return f"● {node.seconds:.1f}s"
