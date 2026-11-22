"""Step hooks may leave dataclass fields unnamed (#15).

adag calls ``run``, ``arun``, ``items``, ``items_data``, ``process_item`` and ``assemble`` with
every dataclass field and every wired input as a keyword. A hook that does not name a field
reads it from ``self`` instead. ``Step.__init_subclass__`` wraps such a hook so the call drops
the fields the hook does not name. Nothing else changes: the step key, ``execute()`` and the
batch item keys still see every field.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import weakref
from collections.abc import Callable
from typing import Any

HOOK_NAMES: tuple[str, ...] = (
    "run",
    "arun",
    "items",
    "items_data",
    "process_item",
    "assemble",
)

_MARK = "__adag_field_filter__"
_FIELD_NAMES: weakref.WeakKeyDictionary[type, frozenset[str]] = weakref.WeakKeyDictionary()


def _field_names(cls: type) -> frozenset[str]:
    """Names of the dataclass fields of ``cls``, computed once per class."""
    names = _FIELD_NAMES.get(cls)
    if names is None:
        found: frozenset[str] = frozenset()
        if dataclasses.is_dataclass(cls):
            found = frozenset(f.name for f in dataclasses.fields(cls))
        names = _FIELD_NAMES[cls] = found
    return names


def _without_unnamed_fields(
    step: object, named: frozenset[str], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Return ``kwargs`` without the fields of ``step`` that the hook does not name."""
    unnamed = _field_names(type(step)) - named
    if unnamed.isdisjoint(kwargs):
        return kwargs
    return {key: value for key, value in kwargs.items() if key not in unnamed}


def takes_any_keyword(hook: Callable[..., Any]) -> bool:
    """Whether ``hook`` has a ``**kwargs`` parameter, so it accepts any keyword."""
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())


def filter_unnamed_fields(hook: Callable[..., Any]) -> Callable[..., Any]:
    """Return ``hook`` wrapped to drop the dataclass fields it does not name.

    A hook with ``**kwargs`` is returned as it is: it receives every field, as before. The
    wrapper keeps ``hook`` as ``__wrapped__``, so ``inspect.signature`` still shows its own
    parameters. A name that is neither a field nor a parameter is not dropped, so Python raises
    the same ``TypeError`` as without the wrapper.
    """
    if getattr(hook, _MARK, False) or takes_any_keyword(hook):
        return hook
    named = frozenset(inspect.signature(hook).parameters)

    if inspect.iscoroutinefunction(hook):

        @functools.wraps(hook)
        async def awrapper(self: object, *args: Any, **kwargs: Any) -> Any:
            return await hook(self, *args, **_without_unnamed_fields(self, named, kwargs))

        wrapper: Callable[..., Any] = awrapper
    else:

        @functools.wraps(hook)
        def swrapper(self: object, *args: Any, **kwargs: Any) -> Any:
            return hook(self, *args, **_without_unnamed_fields(self, named, kwargs))

        wrapper = swrapper
    setattr(wrapper, _MARK, True)
    return wrapper


def wrap_hooks(cls: type) -> None:
    """Wrap each hook that ``cls`` defines itself and that names only some of the fields.

    Only plain functions in ``cls.__dict__`` are wrapped. A hook inherited from a class that is
    not a ``Step`` is not seen here: give it ``**kwargs``, or define the hook on the step.
    """
    for name in HOOK_NAMES:
        hook = cls.__dict__.get(name)
        if inspect.isfunction(hook):
            wrapped = filter_unnamed_fields(hook)
            if wrapped is not hook:
                setattr(cls, name, wrapped)
