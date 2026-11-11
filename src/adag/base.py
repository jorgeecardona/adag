"""CachedStep: a deprecated ``Step`` with direct ``save``/``load`` methods. Removed in 1.0.

Inside a pipeline a ``CachedStep`` is cached like any ``Step``, under the node key (step plus
inputs plus params). The methods below are for code that calls them directly, outside a
pipeline, as the first release allowed::

    step.save(result, cache_dir)
    if step.is_cached(cache_dir):
        result = step.load(cache_dir)

They key on the step hash only (``output_path``), not on the inputs, so two calls with different
inputs share one file. To cache a result that is not one JSON document inside a pipeline,
override ``Step.save_result`` and ``Step.load_result`` instead (see the Caching docs).
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, TypeVar, get_origin

from pydantic import BaseModel

from adag.deprecation import AdagDeprecationWarning
from adag.io import atomic_write_text
from adag.step import Step

CT = TypeVar("CT", bound=BaseModel)


class CachedStep(Step[CT]):
    """Deprecated: a ``Step`` with direct ``save``/``load``/``is_cached`` keyed on the step hash."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if any(get_origin(base) is CachedStep for base in getattr(cls, "__orig_bases__", [])):
            warnings.warn(
                f"{cls.__name__}: CachedStep is deprecated and is removed in adag 1.0. Subclass "
                "Step[...] instead; for a custom cache format override save_result/load_result "
                "(docs/caching.md).",
                AdagDeprecationWarning,
                stacklevel=2,
            )

    def output_path(self, cache_dir: Path) -> Path | None:
        """Return ``cache_dir/<step hash>``, or ``None`` when an input is missing."""
        h = self.content_hash()
        if h is None:
            return None
        return cache_dir.joinpath(h)

    def is_cached(self, cache_dir: Path) -> bool:
        """Return whether the output and its ``.done`` marker exist and ``validate`` passes."""
        output = self.output_path(cache_dir)
        if output is None or not output.exists():
            return False
        if not output.parent.joinpath(output.name + ".done").exists():
            return False
        return self.validate(output)

    def is_partial(self, cache_dir: Path) -> bool:
        """Return whether the output exists without its ``.done`` marker (a crash mid-save)."""
        output = self.output_path(cache_dir)
        if output is None:
            return False
        return output.exists() and not output.parent.joinpath(output.name + ".done").exists()

    def save(self, result: CT, cache_dir: Path) -> None:
        """Write ``result`` as JSON to ``output_path``, then the ``.done`` marker. Both atomic.

        Raises:
            ValueError: an input is missing, so the step has no hash.
        """
        output = self.output_path(cache_dir)
        if output is None:
            raise ValueError(f"Step '{type(self).__name__}' is BLOCKED: inputs missing.")
        atomic_write_text(output, result.model_dump_json(indent=2))
        atomic_write_text(output.parent.joinpath(output.name + ".done"), "")

    def load(self, cache_dir: Path) -> CT:
        """Read the result from ``output_path`` as JSON.

        Raises:
            ValueError: no output, or the step has no ``BaseModel`` result type.
        """
        output = self.output_path(cache_dir)
        result_type = self.result_model()
        if output is None or not output.exists():
            raise ValueError(f"Output for '{type(self).__name__}' not found.")
        if result_type is None:
            raise ValueError(f"Step '{type(self).__name__}' has no result_type.")
        return result_type.model_validate_json(output.read_text())  # pyright: ignore[reportReturnType]

    def validate(self, path: Path) -> bool:
        """Return whether ``path`` parses as the result type. An empty file is not valid."""
        result_type = self.result_model()
        if result_type is None:
            return False
        try:
            content = path.read_text()
            if not content.strip():
                return False
            result_type.model_validate_json(content)
        except Exception:  # any read or parse error means "not valid"
            return False
        return True

    def metadata(self, result: CT) -> dict[str, Any]:
        """Return extra manifest fields for ``result``. Override to add some; default is none."""
        del result
        return {}
