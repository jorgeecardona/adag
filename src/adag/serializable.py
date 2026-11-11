"""SerializableStep — a Step that can be saved to and loaded from disk.

Steps that need serialization inherit from SerializableStep and declare `kind`.
For steps with a spec, pass the spec type to load_spec() in from_directory() —
the TypeVar ensures the return type is inferred correctly without ClassVar overrides.

Example — step with spec:

    class TokenizerSpec(BaseModel):
        model_name: str
        max_length: int = 512

    @dataclass(frozen=True)
    class Tokenizer(SerializableStep[TokenizedInput]):
        kind = "adag/tokenizer/v1"

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

Example — step without spec (state only):

    @dataclass(frozen=True)
    class EmbeddingIndex(SerializableStep[SearchResult]):
        kind = "adag/embedding_index/v1"

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

import inspect
import json
import os
from pathlib import Path
from typing import Any, ClassVar, TypeVar, get_type_hints

from pydantic import BaseModel

from adag.io import atomic_write_text
from adag.step import Out, Step

SpecT = TypeVar("SpecT", bound=BaseModel)


class SerializableStep(Step[Out]):
    """A Step that can be saved to and loaded from disk.

    Subclasses must define `kind`. Use load_spec() in from_directory() to read
    and validate the spec — the TypeVar infers the concrete return type at the call site.
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
        one — readers (`from_directory`, `load_spec`) never see a partial
        JSON.
        """
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
