from __future__ import annotations

from textwrap import dedent

import pytest
from asil_ingest import (
    ParsedFile,
    SourceLanguage,
    TreeSitterParser,
    parse_source,
)


def _parse(src: str, *, path: str = "<inline>", module_name: str | None = None) -> ParsedFile:
    return parse_source(
        dedent(src).lstrip("\n"),
        SourceLanguage.python,
        path=path,
        module_name=module_name,
    )


def test_parses_empty_file() -> None:
    pf = _parse("")
    assert pf.functions == []
    assert pf.classes == []
    assert pf.imports == []
    assert pf.parse_errors == []


def test_extracts_top_level_function() -> None:
    pf = _parse(
        """
        def add(a: int, b: int) -> int:
            '''adds two ints.'''
            return a + b
        """,
        module_name="example",
    )
    assert len(pf.functions) == 1
    fn = pf.functions[0]
    assert fn.name == "add"
    assert fn.qualified_name == "example.add"
    assert fn.signature == "(a: int, b: int) -> int"
    assert fn.docstring == "adds two ints."
    assert fn.start_line == 1
    assert not fn.is_async
    assert not fn.is_method


def test_extracts_async_function() -> None:
    pf = _parse(
        """
        async def fetch(url: str) -> str:
            return await client.get(url)
        """,
        module_name="net",
    )
    assert len(pf.functions) == 1
    fn = pf.functions[0]
    assert fn.is_async
    assert fn.name == "fetch"
    assert fn.qualified_name == "net.fetch"


def test_extracts_class_with_methods() -> None:
    pf = _parse(
        """
        class Greeter(Base):
            '''says hello.'''

            def __init__(self, name: str) -> None:
                self.name = name

            async def greet(self) -> str:
                return f"hi {self.name}"
        """,
        module_name="hello",
    )
    assert len(pf.classes) == 1
    cls = pf.classes[0]
    assert cls.name == "Greeter"
    assert cls.qualified_name == "hello.Greeter"
    assert cls.base_classes == ["Base"]
    assert cls.docstring == "says hello."
    assert [m.name for m in cls.methods] == ["__init__", "greet"]
    assert cls.methods[1].is_async
    assert all(m.is_method for m in cls.methods)
    assert cls.methods[0].parent_class == "hello.Greeter"
    assert cls.methods[0].qualified_name == "hello.Greeter.__init__"


def test_extracts_imports() -> None:
    pf = _parse(
        """
        import os
        import json as j
        from typing import Any, Optional as Opt
        from .siblings import helper
        """,
        module_name="pkg.mod",
    )
    by_module = {imp.module: imp for imp in pf.imports}
    assert "os" in by_module
    assert by_module["os"].names == []
    assert by_module["json"].alias_of == {"j": "json"}
    typing_imp = by_module["typing"]
    assert set(typing_imp.names) == {"Any", "Optional"}
    assert typing_imp.alias_of == {"Opt": "Optional"}
    rel = by_module[".siblings"]
    assert rel.is_relative is True
    assert rel.names == ["helper"]


def test_extracts_calls_inside_function() -> None:
    pf = _parse(
        """
        def outer():
            inner()
            other.method()
            nested(deep(x))
        """,
        module_name="m",
    )
    assert len(pf.functions) == 1
    callees = [c.callee for c in pf.functions[0].calls]
    assert "inner" in callees
    assert "other.method" in callees
    assert "nested" in callees
    assert "deep" in callees


def test_extracts_decorators() -> None:
    pf = _parse(
        """
        @decorator
        @other.decorator(arg=1)
        def wrapped() -> None:
            pass

        @dataclass
        class Holder:
            pass
        """,
        module_name="m",
    )
    assert pf.functions[0].decorators == ["decorator", "other.decorator(arg=1)"]
    assert pf.classes[0].decorators == ["dataclass"]


def test_collects_symbols_with_qualified_names() -> None:
    pf = _parse(
        """
        CONSTANT = 42
        counter = 0

        def fn(): pass

        class C:
            pass
        """,
        module_name="m",
    )
    by_name = {s.name: s for s in pf.symbols}
    assert by_name["CONSTANT"].kind == "constant"
    assert by_name["CONSTANT"].qualified_name == "m.CONSTANT"
    assert by_name["counter"].kind == "variable"
    assert by_name["fn"].kind == "function"
    assert by_name["fn"].qualified_name == "m.fn"
    assert by_name["C"].kind == "class"


def test_records_parse_errors_without_crashing() -> None:
    pf = _parse("def broken( :::  garbage")
    # tree-sitter is permissive; it produces nodes but flags errors
    assert pf.parse_errors  # at least one error recorded
    # parser shouldn't have raised, and we should still get a sensible ParsedFile
    assert pf.language is SourceLanguage.python


def test_loc_count() -> None:
    pf = _parse("a = 1\nb = 2\nc = 3\n")
    assert pf.loc == 4  # 3 lines + trailing newline


def test_non_python_language_raises_until_implemented() -> None:
    with pytest.raises(NotImplementedError, match="Python only"):
        TreeSitterParser(SourceLanguage.typescript)
