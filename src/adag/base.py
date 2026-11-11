"""CachedStep — a Step with content-addressable caching.

CachedStep extends Step[CT] with direct disk caching using a content hash.
Subclasses are frozen dataclasses that declare the result type via the
generic parameter::

    @dataclass(frozen=True)
    class Tokenize(CachedStep[TokenizedText]):
        model_name: str = "my-model"

        def run(self, text: str) -> TokenizedText:
            ...

Unlike step.Step (which caches through the Deferred/Pipeline system),
CachedStep provides direct load/save/is_cached methods for use in
BatchStep workflows and other non-Deferred contexts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, TypeVar, get_args, get_origin

import structlog
from pydantic import BaseModel

from adag.io import atomic_write_text
from adag.step import Step

logger = structlog.get_logger()

CT = TypeVar("CT", bound=BaseModel)


class CachedStep(Step[CT]):
    """A Step with content-addressable disk caching.

    result_type is auto-detected from CachedStep[CT] via __init_subclass__.
    cache_key can be pinned to skip the computed hash. Unknown dataclass field
    types raise at hash time — call out "did you forget to register a type?"
    bugs loudly rather than silently skipping the field.
    """

    strict_content_hash: ClassVar[bool] = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for parent in getattr(cls, "__orig_bases__", []):
            origin = get_origin(parent)
            if origin is CachedStep:
                args = get_args(parent)
                if args and isinstance(args[0], type):
                    cls.result_type = args[0]
                    cls.return_type = args[0]
                    return

    def output_path(self, cache_dir: Path) -> Path | None:
        """Resolved output path: cache_dir/{hash}. None if blocked."""
        h = self.content_hash()
        if h is None:
            return None
        return cache_dir.joinpath(h)

    def is_cached(self, cache_dir: Path) -> bool:
        """True if .done sentinel exists AND output passes validation."""
        output = self.output_path(cache_dir)
        if output is None or not output.exists():
            return False
        done = output.parent.joinpath(output.name + ".done")
        if not done.exists():
            return False
        return self.validate(output)

    def is_partial(self, cache_dir: Path) -> bool:
        """True if output exists but .done is missing."""
        output = self.output_path(cache_dir)
        if output is None:
            return False
        done = output.parent.joinpath(output.name + ".done")
        return output.exists() and not done.exists()

    def save(self, result: CT, cache_dir: Path) -> None:
        """Write result to output_path, then mark the ``.done`` sentinel.

        Both writes are atomic so a crash between them leaves either no
        output (next run recomputes) or a complete-but-unmarked output
        (``is_partial()`` reports ``True``; ``is_cached()`` returns False
        and recomputes). The framework's StepResult cache path uses the
        same two-step write at ``step.py:_done_path``; mirroring it here
        means a direct-API caller's cache is detectable via
        ``is_cached()`` just like the Deferred path.
        """
        output = self.output_path(cache_dir)
        if output is None:
            raise ValueError(f"Step '{type(self).__name__}' is BLOCKED: inputs missing.")
        atomic_write_text(output, result.model_dump_json(indent=2))
        done = output.parent.joinpath(output.name + ".done")
        atomic_write_text(done, "")

    def load(self, cache_dir: Path) -> CT:
        """Read result from output_path. Default: Pydantic JSON."""
        output = self.output_path(cache_dir)
        result_type = self.result_model()
        if output is None or not output.exists():
            raise ValueError(f"Output for '{type(self).__name__}' not found.")
        if result_type is None:
            raise ValueError(f"Step '{type(self).__name__}' has no result_type.")
        return result_type.model_validate_json(output.read_text())  # pyright: ignore[reportReturnType]

    def validate(self, path: Path) -> bool:
        """Validate cached output. Default: parse with result_type."""
        result_type = self.result_model()
        if result_type is None:
            return False
        try:
            result_type.model_validate_json(path.read_text())
            return True
        except Exception:
            return False

    def metadata(self, result: CT) -> dict[str, Any]:
        """Override to add extra fields to the manifest record."""
        return {}
