from __future__ import annotations

from textwrap import dedent

from asil_ingest import (
    ParsedFile,
    SourceLanguage,
    parse_source,
)


def _parse(src: str, *, path: str = "<inline>.ts", module_name: str | None = None) -> ParsedFile:
    return parse_source(
        dedent(src).lstrip("\n"),
        SourceLanguage.typescript,
        path=path,
        module_name=module_name,
    )


def test_extracts_typed_function_declaration() -> None:
    pf = _parse(
        """
        function add(a: number, b: number): number {
            return a + b;
        }
        """,
        module_name="example",
    )
    assert len(pf.functions) == 1
    fn = pf.functions[0]
    assert fn.name == "add"
    assert fn.qualified_name == "example.add"
    assert "number" in fn.signature
    assert fn.signature.endswith(": number") or fn.signature.endswith(":number")


def test_extracts_typed_arrow_function() -> None:
    pf = _parse(
        """
        const greet = (name: string): string => `hi ${name}`;
        """,
        module_name="mod",
    )
    assert len(pf.functions) == 1
    fn = pf.functions[0]
    assert fn.name == "greet"
    assert fn.qualified_name == "mod.greet"
    assert "string" in fn.signature


def test_extracts_class_with_typed_methods() -> None:
    pf = _parse(
        """
        class Counter {
            value: number = 0;
            increment(by: number = 1): number {
                this.value += by;
                return this.value;
            }
            async reset(): Promise<void> {
                this.value = 0;
            }
        }
        """,
        module_name="state",
    )
    assert len(pf.classes) == 1
    cls = pf.classes[0]
    assert cls.name == "Counter"
    assert cls.qualified_name == "state.Counter"
    method_names = [m.name for m in cls.methods]
    assert "increment" in method_names
    assert "reset" in method_names
    reset = next(m for m in cls.methods if m.name == "reset")
    assert reset.is_async
    assert "Promise" in reset.signature


def test_extracts_imports_and_marks_relative() -> None:
    pf = _parse(
        """
        import { Service } from './service';
        import type { Config } from '../config';
        import { Logger as L } from '@pkg/logger';
        """,
        module_name="src.app",
    )
    by_module = {imp.module: imp for imp in pf.imports}
    assert by_module["./service"].is_relative is True
    assert "Service" in by_module["./service"].names
    assert by_module["../config"].is_relative is True
    assert by_module["@pkg/logger"].is_relative is False
    assert by_module["@pkg/logger"].alias_of["L"] == "Logger"


def test_skips_interface_and_type_alias_declarations() -> None:
    pf = _parse(
        """
        interface User {
            id: number;
            name: string;
        }
        type Id = string | number;
        enum Color { Red, Green, Blue }
        function useUser(u: User): Id {
            return u.id;
        }
        """,
        module_name="m",
    )
    fn_names = [fn.name for fn in pf.functions]
    assert "useUser" in fn_names
    # Step-1 scope cut: interface / type / enum are intentionally NOT captured
    # as classes or symbols. The capture function does still land.
    class_names = [c.name for c in pf.classes]
    assert "User" not in class_names


def test_records_parse_errors_without_crashing() -> None:
    pf = _parse("function broken(: garbage")
    assert pf.parse_errors
    assert pf.language is SourceLanguage.typescript
