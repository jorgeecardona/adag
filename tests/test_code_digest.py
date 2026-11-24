"""The code part of a key is a digest of the class syntax tree, not of its text (#19)."""

import pytest

from adag import hashing
from adag.hashing import _syntax_digest, class_code_digest  # pyright: ignore[reportPrivateUsage]

# One class with the syntax that differs most between Python versions. It is indented, as a class
# defined inside a function is, and holds a docstring line with no indent.
GOLDEN_SOURCE = '''\
    @dataclass(frozen=True)
    class Gen[T: int](Step[Num], metaclass=M):
        """Doc."""
        a: int = 1
        b: "list[str]" = ()
        type Alias[U] = dict[str, U]

        async def run[V](self, a: int, *args, b: str = "x", **kw) -> Num:
            """Doc.
zero indent line
            """
            # comment
            x = f"{a!r:>{b}} {a=} \u00e9\\x85"
            y = [i for i in range(3) if i] + [*args]
            match a:
                case {"k": [1, *rest]} | Num(value=2) if x: pass
                case _: return (yield)
            try:
                pass
            except* ValueError as e:
                raise
            lambda q, /, r=1.5e3, *, s: (q, 1j, ..., b"b", -0.0, None, True)
            with open(a) as f, open(b): del f
            global g
            import os.path as op
            from . import helper
            return Num(value=a)

        class Out(BaseModel):
            """Schema text."""
'''

# The same digest must come out on every supported Python. If this fails on one interpreter only,
# the walk depends on that version: fix the walk, do not change the literal.
GOLDEN_DIGEST = b"f9d014a39015"


def test_golden_digest_is_the_same_on_every_python() -> None:
    assert _syntax_digest(GOLDEN_SOURCE) == GOLDEN_DIGEST


BASE = '''\
class A(Step[Num]):
    """First line."""
    a: int = 1

    def run(self, a: int) -> Num:
        # keep going
        return Num(value=a)
'''


def _same(other: str) -> bool:
    return _syntax_digest(other) == _syntax_digest(BASE)


@pytest.mark.parametrize(
    "edit",
    [
        BASE.replace("# keep going", "# another comment"),
        BASE.replace("    # keep going\n", ""),
        BASE.replace('"""First line."""', '"""Rewritten."""'),
        BASE.replace('    """First line."""\n', ""),
        BASE.replace("\n    a: int = 1", "\n\n\n    a:  int  =  1"),
        BASE.replace("Num(value=a)", "Num(\n            value=a,\n        )"),
        BASE.replace("Num(value=a)", "(Num(value=a))"),
    ],
    ids=[
        "comment text",
        "comment removed",
        "docstring text",
        "docstring removed",
        "spacing",
        "wrapped call",
        "extra parentheses",
    ],
)
def test_an_edit_that_changes_no_statement_keeps_the_digest(edit: str) -> None:
    assert _same(edit)


@pytest.mark.parametrize(
    "edit",
    [
        BASE.replace("a: int = 1", "a: int = 2"),
        BASE.replace("a: int = 1", "a: float = 1"),
        BASE.replace("-> Num", "-> Other"),
        BASE.replace("value=a", "value=a + 1"),
        BASE.replace("Step[Num]", "Step[Other]"),
        BASE.replace("class A(", "@frozen\nclass A("),
        BASE.replace("def run", "async def run"),
        BASE.replace("return Num(value=a)", "x = 1\n        return Num(value=a)"),
    ],
    ids=[
        "default",
        "field annotation",
        "return annotation",
        "statement",
        "base",
        "decorator",
        "async",
        "added statement",
    ],
)
def test_an_edit_that_changes_a_statement_moves_the_digest(edit: str) -> None:
    assert not _same(edit)


def test_a_docstring_the_code_reads_stays_in_the_digest() -> None:
    source = BASE.replace("return Num(value=a)", "return Num(value=len(self.__doc__))")
    edited = source.replace("First line.", "Second line.")
    assert _syntax_digest(source) != _syntax_digest(edited)


def test_a_nested_class_docstring_stays_in_the_digest() -> None:
    source = 'class A:\n    class Out:\n        """One."""\n        x: int\n'
    assert _syntax_digest(source) != _syntax_digest(source.replace("One.", "Two."))


def test_an_import_in_a_method_gives_the_same_digest_on_every_python() -> None:
    # Python 3.15 adds is_lazy=0 to imports. The golden source has imports too.
    source = "class A:\n    def run(self):\n        import os\n        return 1\n"
    assert _syntax_digest(source) == b"5e5a31c873f7"


def test_the_method_does_not_depend_on_the_depth_of_the_caller() -> None:
    branches = "".join(f"        elif x == {i}:\n            y = {i}\n" for i in range(1, 400))
    source = f"class A:\n    def run(self, x):\n        if x == 0:\n            y = 0\n{branches}"
    shallow = _syntax_digest(source)
    assert shallow is not None

    def deep(n: int) -> bytes | None:
        return _syntax_digest(source) if n == 0 else deep(n - 1)

    assert deep(600) == shallow


def test_an_indented_class_with_a_zero_indent_docstring_line_parses() -> None:
    source = '    class A:\n        """x\nzero\n        """\n        pass\n'
    assert _syntax_digest(source) is not None


def test_the_digest_is_twelve_hex_characters() -> None:
    digest = _syntax_digest(BASE)
    assert digest is not None
    assert len(digest) == 12
    int(digest, 16)


def test_source_that_does_not_parse_has_no_digest() -> None:
    assert _syntax_digest("class A(:\n") is None
    assert _syntax_digest("x = 1\n") is None


def test_a_class_whose_source_does_not_parse_falls_back_to_the_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Odd:
        pass

    def broken_source(_cls: object) -> str:
        return "class Odd(:\n"

    monkeypatch.setattr(hashing.inspect, "getsource", broken_source)
    hashing._class_source_hash_cache.pop(Odd, None)  # pyright: ignore[reportPrivateUsage]
    found = class_code_digest(Odd)
    assert found is not None
    assert found[0] == "source"
    assert len(found[1]) == 12


def test_a_class_with_source_reports_the_ast_method() -> None:
    class Plain:
        def run(self) -> int:
            return 1

    found = class_code_digest(Plain)
    assert found is not None
    assert found[0] == "ast"
