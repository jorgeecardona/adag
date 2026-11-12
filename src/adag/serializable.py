"""SerializableStep: a Step that ``Pipeline.save`` writes and ``Pipeline.from_directory`` rebuilds.

Subclass ``SerializableStep`` and set ``kind``. Override ``from_directory`` only when the step
has a required field other than ``spec``. Then pass the spec type to ``load_spec()``: the
TypeVar infers the return type without ClassVar overrides.

Example, a step with a spec:

    class TokenizerSpec(BaseModel):
        model_name: str
        max_length: int = 512

    @dataclass(frozen=True)
    class Tokenizer(SerializableStep[TokenizedInput]):
        kind = "example/tokenizer/v1"

        spec: TokenizerSpec
        tokenizer: PreTrainedTokenizerBase

        def save_artifacts(self, path: str) -> None:
            super().save_artifacts(path)              # writes step.json with kind + spec
            self.tokenizer.save_pretrained(path)

        @classmethod
        def from_directory(cls, path: str) -> Tokenizer:
            spec = cls.load_spec(path, TokenizerSpec) # returns TokenizerSpec, fully typed
            tokenizer = load_tokenizer(path)
            return cls(spec=spec, tokenizer=tokenizer)

Example, a step without a spec (state only):

    @dataclass(frozen=True)
    class EmbeddingIndex(SerializableStep[SearchResult]):
        kind = "example/embedding_index/v1"

        index: faiss.Index

        def save_artifacts(self, path: str) -> None:
            super().save_artifacts(path)              # writes step.json with kind only
            faiss.write_index(self.index, os.path.join(path, "index.faiss"))

        @classmethod
        def from_directory(cls, path: str) -> EmbeddingIndex:
            cls.assert_kind(path)
            index = faiss.read_index(os.path.join(path, "index.faiss"))
            return cls(index=index)
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
from pathlib import Path
from typing import Any, ClassVar, Self, TypeVar, get_type_hints

from pydantic import BaseModel

from adag.io import atomic_write_text
from adag.step import Out, Step

SpecT = TypeVar("SpecT", bound=BaseModel)


class SerializableStep(Step[Out]):
    """A Step that ``Pipeline.save`` writes to disk and ``Pipeline.from_directory`` rebuilds.

    The contract a subclass implements:

    - ``kind``: a non-empty class attribute, for example ``"my-tokenizer/v1"``. The registry passed
      to ``Pipeline.from_directory`` maps it back to the class. Raised at class definition if
      missing.
    - ``spec``: an optional dataclass field holding a ``BaseModel``. ``save_artifacts`` writes it
      to ``step.json``.
    - ``from_directory(cls, path)``: a classmethod that returns the step saved at ``path``. The
      default rebuilds a step whose fields are ``spec`` plus fields with defaults. A step with any
      other required field must override it, usually with ``cls(spec=cls.load_spec(path, Spec))``.
      Saving a step that cannot be rebuilt raises ``TypeError`` at save time, not at load time.
    - ``save_artifacts(self, path)``: override only to write extra state (weights, tokenizer
      files) next to ``step.json``. Call ``super().save_artifacts(path)`` first.
    """

    kind: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Enforce at class definition time that kind is set and spec is a BaseModel."""
        super().__init_subclass__(**kwargs)
        if "kind" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} must define 'kind'")
        if not cls.kind:
            raise TypeError(f"{cls.__name__}.kind must be a non-empty string")
        if "spec" in inspect.get_annotations(cls):
            try:
                hints = get_type_hints(cls)
                spec_type = hints.get("spec")
                if isinstance(spec_type, type) and not issubclass(spec_type, BaseModel):
                    raise TypeError(
                        f"{cls.__name__}.spec must be annotated with a BaseModel subclass, got {spec_type.__name__}"
                    )
            except NameError:
                pass

    def save_artifacts(self, path: str) -> None:
        """Write step.json with kind and spec (if a spec field exists). Override to add state.

        Atomic via ``atomic_write_text`` so a crash during ``Pipeline.save``
        leaves either the previous step.json intact or the complete new
        one: readers (`from_directory`, `load_spec`) never see a partial
        JSON.
        """
        type(self)._check_loadable()
        os.makedirs(path, exist_ok=True)
        spec_data: dict[str, Any] = {}
        spec_field = getattr(self, "spec", None)
        if isinstance(spec_field, BaseModel):
            spec_data = spec_field.model_dump()
        atomic_write_text(
            Path(path).joinpath("step.json"),
            json.dumps({"kind": self.kind, "spec": spec_data}, indent=2) + "\n",
        )

    @classmethod
    def from_directory(cls, path: str) -> Self:
        """Return the step saved at ``path``. Override when the default cannot rebuild it.

        The default reads ``step.json``, checks ``kind``, and passes ``spec`` (when the class has
        that field) to the constructor. Every other field keeps its default.
        """
        cls._check_loadable()
        hints = get_type_hints(cls)
        spec_type = hints.get("spec")
        if isinstance(spec_type, type) and issubclass(spec_type, BaseModel):
            spec: Any = cls.load_spec(path, spec_type)
            return cls(spec=spec)  # pyright: ignore[reportCallIssue]
        cls.assert_kind(path)
        return cls()

    @classmethod
    def _check_loadable(cls) -> None:
        """Raise ``TypeError`` when the default ``from_directory`` cannot rebuild this class."""
        if "from_directory" in _own_attrs(cls):
            return
        missing = [
            f.name
            for f in dataclasses.fields(cls)  # pyright: ignore[reportArgumentType]
            if f.name != "spec"
            and f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING
        ]
        if missing:
            raise TypeError(
                f"{cls.__name__} cannot be loaded back: field(s) {', '.join(missing)} have no "
                "default and are not 'spec'. Override from_directory(cls, path) to rebuild it."
            )

    @classmethod
    def load_spec(cls, path: str, spec_type: type[SpecT]) -> SpecT:
        """Read step.json, assert kind matches, and return the validated spec."""
        data = json.loads(Path(path).joinpath("step.json").read_text())
        if data.get("kind") != cls.kind:
            raise ValueError(f"Expected kind {cls.kind!r}, got {data.get('kind')!r}")
        return spec_type.model_validate(data["spec"])

    @classmethod
    def assert_kind(cls, path: str) -> None:
        """Read step.json and assert the kind matches. For steps without a spec."""
        data = json.loads(Path(path).joinpath("step.json").read_text())
        if data.get("kind") != cls.kind:
            raise ValueError(f"Expected kind {cls.kind!r}, got {data.get('kind')!r}")


def _own_attrs(cls: type) -> set[str]:
    """Names defined on ``cls`` or a parent below ``SerializableStep`` in the MRO."""
    names: set[str] = set()
    for klass in cls.__mro__:
        if klass is SerializableStep:
            break
        names.update(vars(klass))
    return names
