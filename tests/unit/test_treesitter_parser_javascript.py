from __future__ import annotations

from textwrap import dedent

from asil_ingest import (
    ParsedFile,
    SourceLanguage,
    parse_source,
)


def _parse(src: str, *, path: str = "<inline>.js", module_name: str | None = None) -> ParsedFile:
    return parse_source(
        dedent(src).lstrip("\n"),
        SourceLanguage.javascript,
        path=path,
        module_name=module_name,
    )


def test_extracts_top_level_function_declaration() -> None:
    pf = _parse(
        """
        function add(a, b) {
            return a + b;
        }
        """,
        module_name="example",
    )
    assert len(pf.functions) == 1
    fn = pf.functions[0]
    assert fn.name == "add"
    assert fn.qualified_name == "example.add"
    assert fn.signature == "(a, b)"
    assert fn.start_line == 1
    assert not fn.is_async
    assert not fn.is_method


def test_extracts_named_arrow_function() -> None:
    pf = _parse(
        """
        const bar = (x) => {
            return x * 2;
        };
        """,
        module_name="mod",
    )
    assert len(pf.functions) == 1
    fn = pf.functions[0]
    assert fn.name == "bar"
    assert fn.qualified_name == "mod.bar"
    assert fn.signature == "(x)"
    assert not fn.is_async


def test_extracts_async_arrow_function() -> None:
    pf = _parse(
        """
        const fetchUser = async (id) => {
            const r = await fetch(`/u/${id}`);
            return r.json();
        };
        """,
        module_name="net",
    )
    assert len(pf.functions) == 1
    fn = pf.functions[0]
    assert fn.is_async
    assert fn.name == "fetchUser"
    callees = [c.callee for c in fn.calls]
    # template literal in the argument shouldn't crash; the call is to `fetch`
    assert "fetch" in callees
    assert any("json" in c for c in callees)


def test_extracts_class_with_methods() -> None:
    pf = _parse(
        """
        class Greeter extends Base {
            constructor(name) {
                this.name = name;
            }
            async greet() {
                return `hi ${this.name}`;
            }
        }
        """,
        module_name="hello",
    )
    assert len(pf.classes) == 1
    cls = pf.classes[0]
    assert cls.name == "Greeter"
    assert cls.qualified_name == "hello.Greeter"
    assert cls.base_classes == ["Base"]
    method_names = [m.name for m in cls.methods]
    assert "constructor" in method_names
    assert "greet" in method_names
    greet = next(m for m in cls.methods if m.name == "greet")
    assert greet.is_async
    assert greet.is_method
    assert greet.parent_class == "hello.Greeter"
    assert greet.qualified_name == "hello.Greeter.greet"


def test_extracts_imports_named_default_and_namespace() -> None:
    pf = _parse(
        """
        import React from 'react';
        import { useState, useEffect as useFx } from 'react';
        import * as utils from './utils';
        import 'side-effect-only';
        """,
        module_name="src.app",
    )
    react_imports = [imp for imp in pf.imports if imp.module == "react"]
    assert len(react_imports) == 2
    default_imp = next(imp for imp in react_imports if imp.alias_of.get("React") == "default")
    assert default_imp.names == ["React"]
    named = next(imp for imp in react_imports if "useState" in imp.names)
    assert "useEffect" in named.names
    assert named.alias_of["useFx"] == "useEffect"
    utils = next(imp for imp in pf.imports if imp.module == "./utils")
    assert utils.is_relative is True
    assert utils.alias_of.get("utils") == "*"
    assert pf.imports[-1].module == "side-effect-only"


def test_extracts_call_sites_inside_function() -> None:
    pf = _parse(
        """
        function outer() {
            inner();
            other.method();
            nested(deep(x));
        }
        """,
        module_name="m",
    )
    assert len(pf.functions) == 1
    callees = [c.callee for c in pf.functions[0].calls]
    assert "inner" in callees
    assert "other.method" in callees
    assert "nested" in callees
    assert "deep" in callees


def test_handles_export_statements() -> None:
    pf = _parse(
        """
        export function add(a, b) { return a + b; }
        export const sub = (a, b) => a - b;
        export class Calc {
            mul(a, b) { return a * b; }
        }
        export default function main() { return 0; }
        """,
        module_name="lib",
    )
    fn_names = {fn.name for fn in pf.functions}
    assert {"add", "sub", "main"}.issubset(fn_names)
    assert pf.classes[0].name == "Calc"
    assert "mul" in [m.name for m in pf.classes[0].methods]


def test_records_parse_errors_without_crashing() -> None:
    pf = _parse("function broken( :::  garbage")
    assert pf.parse_errors  # at least one error recorded
    assert pf.language is SourceLanguage.javascript


def test_loc_count() -> None:
    pf = _parse("const a = 1;\nconst b = 2;\nconst c = 3;\n")
    assert pf.loc == 4


def test_collects_symbols_for_top_level_const_and_var() -> None:
    pf = _parse(
        """
        const PI = 3.14;
        let counter = 0;
        function fn() {}
        class C {}
        """,
        module_name="m",
    )
    by_name = {s.name: s for s in pf.symbols}
    assert by_name["PI"].kind == "constant"
    assert by_name["PI"].qualified_name == "m.PI"
    assert by_name["counter"].kind == "variable"
    assert by_name["fn"].kind == "function"
    assert by_name["C"].kind == "class"
