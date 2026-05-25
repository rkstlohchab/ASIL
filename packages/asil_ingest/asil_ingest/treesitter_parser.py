"""Tree-sitter-based parser producing typed `ParsedFile` records.

Phase 1 step 1.1 shipped Python. Phase 1 step 1.8 adds JS / TS / TSX.

JS / TS / TSX coverage is intentionally narrow:
  - Captured: function declarations, named arrow functions
    (`const foo = () => {...}`), classes + methods, ES module imports,
    call sites inside function bodies.
  - Deferred: interfaces, type aliases, enums, declaration merging,
    `export default <anonymous>`, dynamic imports, decorators, JSDoc
    docstrings, JSX-specific extraction (TSX still parses but JSX
    structure is not exposed beyond the host function).

The deferrals are scoped to step 1; the moat work (causality, replay,
drift) is language-agnostic at the graph schema level.

Design notes:
  - The parser is intentionally permissive — tree-sitter never raises, it just
    marks nodes with `has_error`. We record those in `ParsedFile.parse_errors`
    rather than refusing to ingest a partially-broken file.
  - Qualified names are computed inside this module so a downstream caller can
    look up `Class.method` without knowing how qualified names are formed.
  - We use `tree-sitter-language-pack` for prebuilt binaries. Its binding
    exposes a Rust-backed Parser/Node where every accessor is a METHOD (not
    a property). The small `_node` shim at the bottom of this file isolates
    that quirk so the per-language extractors read naturally.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from asil_ingest.models import (
    ParsedCall,
    ParsedClass,
    ParsedFile,
    ParsedFunction,
    ParsedImport,
    ParsedSymbol,
    SourceLanguage,
)


@cache
def _get_parser(language: SourceLanguage) -> Any:
    """Lazy-load and cache one tree-sitter parser per language."""
    from tree_sitter_language_pack import get_parser

    return get_parser(language.value)


def parse_source(
    source: str | bytes,
    language: SourceLanguage,
    *,
    path: str = "<inline>",
    module_name: str | None = None,
) -> ParsedFile:
    """Convenience wrapper — one-shot parse without instantiating a parser."""
    parser = TreeSitterParser(language)
    return parser.parse(source, path=path, module_name=module_name)


_SUPPORTED_LANGUAGES: frozenset[SourceLanguage] = frozenset(
    {
        SourceLanguage.python,
        SourceLanguage.typescript,
        SourceLanguage.javascript,
        SourceLanguage.tsx,
        SourceLanguage.go,
        SourceLanguage.ruby,
        SourceLanguage.java,
        SourceLanguage.rust,
        SourceLanguage.c,
        SourceLanguage.cpp,
        SourceLanguage.php,
        SourceLanguage.swift,
        SourceLanguage.kotlin,
    }
)


# Per-language node-type / field-name configuration for the generic structural
# extractor used by every non-Python, non-JS-family language. The extractor is
# intentionally coarse — it finds function-like and class-like declarations at
# any nesting depth, harvests imports, and records every call expression. Each
# language has subtle quirks the generic walker cannot capture (decorators,
# docstrings, sophisticated dispatch); those are accepted gaps for v1.
_GENERIC_LANG_CONFIG: dict[SourceLanguage, dict[str, Any]] = {
    SourceLanguage.go: {
        # type_declaration wraps one or more type_specs; the name lives on
        # type_spec, so we treat the spec itself as the class-like node.
        "function_kinds": {"function_declaration", "method_declaration"},
        "class_kinds": {"type_spec"},
        "import_kinds": {"import_declaration"},
        "import_spec_kinds": {"import_spec"},
        "call_kinds": {"call_expression"},
        "name_field": "name",
        "class_name_field": "name",
        "function_callee_field": "function",
    },
    SourceLanguage.ruby: {
        "function_kinds": {"method", "singleton_method"},
        "class_kinds": {"class", "module"},
        "import_kinds": {"call"},  # `require 'x'` is parsed as a call
        "import_callee_names": {"require", "require_relative", "load"},
        "call_kinds": {"call"},
        "name_field": "name",
        "function_callee_field": "method",
    },
    SourceLanguage.java: {
        "function_kinds": {"method_declaration", "constructor_declaration"},
        "class_kinds": {"class_declaration", "interface_declaration", "record_declaration", "enum_declaration"},
        "import_kinds": {"import_declaration"},
        "call_kinds": {"method_invocation", "object_creation_expression"},
        "name_field": "name",
        "function_callee_field": "name",
    },
    SourceLanguage.rust: {
        "function_kinds": {"function_item"},
        "class_kinds": {"struct_item", "enum_item", "trait_item", "impl_item"},
        "import_kinds": {"use_declaration"},
        "call_kinds": {"call_expression", "macro_invocation"},
        "name_field": "name",
        "function_callee_field": "function",
    },
    SourceLanguage.c: {
        # C/C++ functions hide their name under
        # function_definition.declarator (a function_declarator wrapper).
        # Classes / structs put their name on the `name` field directly.
        "function_kinds": {"function_definition"},
        "class_kinds": {"struct_specifier", "union_specifier", "enum_specifier"},
        "import_kinds": {"preproc_include"},
        "call_kinds": {"call_expression"},
        "name_field": "declarator",
        "class_name_field": "name",
        "function_callee_field": "function",
    },
    SourceLanguage.cpp: {
        "function_kinds": {"function_definition"},
        "class_kinds": {"class_specifier", "struct_specifier", "namespace_definition", "union_specifier", "enum_specifier"},
        "import_kinds": {"preproc_include", "using_declaration"},
        "call_kinds": {"call_expression"},
        "name_field": "declarator",
        "class_name_field": "name",
        "function_callee_field": "function",
    },
    SourceLanguage.php: {
        "function_kinds": {"function_definition", "method_declaration"},
        "class_kinds": {"class_declaration", "interface_declaration", "trait_declaration"},
        "import_kinds": {"namespace_use_declaration"},
        "call_kinds": {"function_call_expression", "member_call_expression", "scoped_call_expression"},
        "name_field": "name",
        "function_callee_field": "function",
    },
    SourceLanguage.swift: {
        "function_kinds": {"function_declaration", "init_declaration"},
        "class_kinds": {"class_declaration", "protocol_declaration", "extension_declaration"},
        "import_kinds": {"import_declaration"},
        "call_kinds": {"call_expression"},
        "name_field": "name",
        "function_callee_field": "function",
    },
    SourceLanguage.kotlin: {
        "function_kinds": {"function_declaration"},
        "class_kinds": {"class_declaration", "object_declaration"},
        "import_kinds": {"import_header"},
        "call_kinds": {"call_expression"},
        "name_field": "name",
        "function_callee_field": "function",
    },
}


class TreeSitterParser:
    def __init__(self, language: SourceLanguage) -> None:
        self.language = language
        if language not in _SUPPORTED_LANGUAGES:
            raise NotImplementedError(
                f"{language} is not yet implemented; supported: "
                f"{sorted(lang.value for lang in _SUPPORTED_LANGUAGES)}."
            )
        self._parser = _get_parser(language)

    def parse(
        self,
        source: str | bytes,
        *,
        path: str = "<inline>",
        module_name: str | None = None,
    ) -> ParsedFile:
        src_str = source.decode("utf-8") if isinstance(source, bytes) else source
        src_bytes = src_str.encode("utf-8")
        tree = self._parser.parse(src_str)
        root = tree.root_node()

        loc = src_str.count("\n") + 1
        parsed = ParsedFile(
            path=path,
            language=self.language,
            module_name=module_name,
            loc=loc,
        )
        if _has_error(root):
            parsed.parse_errors.append("tree-sitter reported syntax errors during parse")

        mod = module_name or _stem(path)
        if self.language is SourceLanguage.python:
            self._parse_python(root, src_bytes, parsed, mod)
        elif self.language is SourceLanguage.typescript:
            self._parse_typescript(root, src_bytes, parsed, mod)
        elif self.language is SourceLanguage.javascript:
            self._parse_javascript(root, src_bytes, parsed, mod)
        elif self.language is SourceLanguage.tsx:
            self._parse_tsx(root, src_bytes, parsed, mod)
        elif self.language in _GENERIC_LANG_CONFIG:
            self._parse_generic(root, src_bytes, parsed, mod, _GENERIC_LANG_CONFIG[self.language])
        return parsed

    # ------------------------------------------------------------------ python

    def _parse_python(self, root: Any, src: bytes, parsed: ParsedFile, mod: str) -> None:
        for child in _named_children(root):
            self._dispatch_python_top_level(child, src, parsed, mod)

    def _dispatch_python_top_level(
        self, node: Any, src: bytes, parsed: ParsedFile, mod: str
    ) -> None:
        t = _kind(node)
        if t == "import_statement":
            parsed.imports.extend(self._py_import(node, src, relative=False))
        elif t == "import_from_statement":
            parsed.imports.extend(self._py_import_from(node, src))
        elif t == "function_definition":
            fn = self._py_function(node, src, mod, parent_class=None, decorators=[])
            parsed.functions.append(fn)
            parsed.symbols.append(_symbol(fn.name, "function", fn.start_line, fn.qualified_name))
        elif t == "class_definition":
            cls = self._py_class(node, src, mod, decorators=[])
            parsed.classes.append(cls)
            parsed.symbols.append(_symbol(cls.name, "class", cls.start_line, cls.qualified_name))
        elif t == "decorated_definition":
            decorators = self._py_decorators(node, src)
            inner = node.child_by_field_name("definition")
            if inner is None:
                return
            inner_kind = _kind(inner)
            if inner_kind == "function_definition":
                fn = self._py_function(inner, src, mod, parent_class=None, decorators=decorators)
                parsed.functions.append(fn)
                parsed.symbols.append(
                    _symbol(fn.name, "function", fn.start_line, fn.qualified_name)
                )
            elif inner_kind == "class_definition":
                cls = self._py_class(inner, src, mod, decorators=decorators)
                parsed.classes.append(cls)
                parsed.symbols.append(
                    _symbol(cls.name, "class", cls.start_line, cls.qualified_name)
                )
        elif t == "expression_statement":
            # module docstring or top-level expression — ignored for now
            pass
        elif t == "assignment":
            name_node = node.child_by_field_name("left")
            if name_node is not None and _kind(name_node) == "identifier":
                name = _text(name_node, src)
                kind = "constant" if name.isupper() else "variable"
                parsed.symbols.append(_symbol(name, kind, _start_row(name_node), f"{mod}.{name}"))

    def _py_function(
        self,
        node: Any,
        src: bytes,
        mod: str,
        *,
        parent_class: str | None,
        decorators: list[str],
    ) -> ParsedFunction:
        name_node = node.child_by_field_name("name")
        name = _text(name_node, src) if name_node else "<anonymous>"
        qualified = f"{parent_class}.{name}" if parent_class else f"{mod}.{name}"
        params_node = node.child_by_field_name("parameters")
        params = _text(params_node, src) if params_node else "()"
        return_node = node.child_by_field_name("return_type")
        return_type = f" -> {_text(return_node, src)}" if return_node else ""
        signature = f"{params}{return_type}"

        body = node.child_by_field_name("body")
        docstring = self._py_docstring(body, src)
        calls = self._py_calls(body, src) if body is not None else []

        is_async = any(_kind(c) == "async" for c in _children(node))

        return ParsedFunction(
            name=name,
            qualified_name=qualified,
            start_line=_start_row(node),
            end_line=_end_row(node),
            signature=signature,
            docstring=docstring,
            is_async=is_async,
            is_method=parent_class is not None,
            parent_class=parent_class,
            calls=calls,
            decorators=decorators,
        )

    def _py_class(self, node: Any, src: bytes, mod: str, *, decorators: list[str]) -> ParsedClass:
        name_node = node.child_by_field_name("name")
        name = _text(name_node, src) if name_node else "<anonymous>"
        qualified = f"{mod}.{name}"

        bases_node = node.child_by_field_name("superclasses")
        base_classes: list[str] = []
        if bases_node is not None:
            for child in _named_children(bases_node):
                if _kind(child) != "keyword_argument":
                    base_classes.append(_text(child, src))

        body = node.child_by_field_name("body")
        docstring = self._py_docstring(body, src)

        methods: list[ParsedFunction] = []
        if body is not None:
            for child in _named_children(body):
                ck = _kind(child)
                if ck == "function_definition":
                    methods.append(
                        self._py_function(child, src, mod, parent_class=qualified, decorators=[])
                    )
                elif ck == "decorated_definition":
                    inner_decorators = self._py_decorators(child, src)
                    inner = child.child_by_field_name("definition")
                    if inner is not None and _kind(inner) == "function_definition":
                        methods.append(
                            self._py_function(
                                inner,
                                src,
                                mod,
                                parent_class=qualified,
                                decorators=inner_decorators,
                            )
                        )

        return ParsedClass(
            name=name,
            qualified_name=qualified,
            start_line=_start_row(node),
            end_line=_end_row(node),
            docstring=docstring,
            base_classes=base_classes,
            methods=methods,
            decorators=decorators,
        )

    def _py_import(self, node: Any, src: bytes, *, relative: bool) -> list[ParsedImport]:
        """Handles `import foo`, `import foo as f`, `import foo, bar`."""
        line = _start_row(node)
        out: list[ParsedImport] = []
        for child in _named_children(node):
            ck = _kind(child)
            if ck == "dotted_name":
                out.append(
                    ParsedImport(
                        module=_text(child, src),
                        names=[],
                        line=line,
                        is_relative=relative,
                    )
                )
            elif ck == "aliased_import":
                name_node = child.child_by_field_name("name")
                alias_node = child.child_by_field_name("alias")
                if name_node is None:
                    continue
                mod = _text(name_node, src)
                alias = _text(alias_node, src) if alias_node else None
                imp = ParsedImport(module=mod, names=[], line=line, is_relative=relative)
                if alias:
                    imp.alias_of[alias] = mod
                out.append(imp)
        return out

    def _py_import_from(self, node: Any, src: bytes) -> list[ParsedImport]:
        """Handles `from foo import a, b as c` and `from .foo import bar`."""
        line = _start_row(node)
        module_node = node.child_by_field_name("module_name")
        # tree-sitter-language-pack returns a fresh Python wrapper for each
        # node access, so `child is module_node` never matches even when both
        # point at the same C node. Compare by byte offset instead.
        module_start = module_node.start_byte() if module_node is not None else -1
        is_relative = False
        module_text = ""
        if module_node is not None:
            module_text = _text(module_node, src)
            is_relative = module_text.startswith(".")

        names: list[str] = []
        alias_of: dict[str, str] = {}
        for child in _named_children(node):
            if child.start_byte() == module_start:
                continue
            ck = _kind(child)
            if ck == "dotted_name":
                names.append(_text(child, src))
            elif ck == "aliased_import":
                name_node = child.child_by_field_name("name")
                alias_node = child.child_by_field_name("alias")
                if name_node is not None:
                    original = _text(name_node, src)
                    names.append(original)
                    if alias_node is not None:
                        alias_of[_text(alias_node, src)] = original
            elif ck == "wildcard_import":
                names.append("*")

        return [
            ParsedImport(
                module=module_text,
                names=names,
                alias_of=alias_of,
                line=line,
                is_relative=is_relative,
            )
        ]

    def _py_docstring(self, body: Any, src: bytes) -> str | None:
        if body is None:
            return None
        children = _named_children(body)
        if not children:
            return None
        first = children[0]
        # The python grammar nests the docstring differently in different
        # versions: sometimes `string` is the direct first child of `block`,
        # sometimes wrapped in `expression_statement`. Handle both.
        if _kind(first) == "expression_statement":
            inner_children = _named_children(first)
            if not inner_children:
                return None
            first = inner_children[0]
        if _kind(first) != "string":
            return None
        # `string` itself has named children: string_start, string_content,
        # string_end. Prefer the content child for a clean strip.
        for child in _named_children(first):
            if _kind(child) == "string_content":
                return _text(child, src)
        return _strip_python_string_quotes(_text(first, src))

    def _py_calls(self, body: Any, src: bytes) -> list[ParsedCall]:
        out: list[ParsedCall] = []
        for node in _walk(body):
            if _kind(node) != "call":
                continue
            fn_node = node.child_by_field_name("function")
            if fn_node is None:
                continue
            out.append(ParsedCall(callee=_text(fn_node, src), line=_start_row(node)))
        return out

    def _py_decorators(self, decorated_node: Any, src: bytes) -> list[str]:
        out: list[str] = []
        for child in _children(decorated_node):
            if _kind(child) == "decorator":
                # `@name` or `@name(args)` — strip the leading '@'
                text = _text(child, src).lstrip("@").strip()
                out.append(text)
        return out

    # ------------------------------------------------------------ js / ts / tsx

    def _parse_typescript(self, root: Any, src: bytes, parsed: ParsedFile, mod: str) -> None:
        self._parse_js_family(root, src, parsed, mod)

    def _parse_javascript(self, root: Any, src: bytes, parsed: ParsedFile, mod: str) -> None:
        self._parse_js_family(root, src, parsed, mod)

    def _parse_tsx(self, root: Any, src: bytes, parsed: ParsedFile, mod: str) -> None:
        # TSX is TypeScript + JSX. The tree-sitter grammar for tsx covers both;
        # JSX expressions appear as plain expressions inside function bodies, so
        # the host function/class extraction logic is identical.
        self._parse_js_family(root, src, parsed, mod)

    def _parse_js_family(self, root: Any, src: bytes, parsed: ParsedFile, mod: str) -> None:
        for child in _named_children(root):
            self._dispatch_js_top_level(child, src, parsed, mod)

    def _dispatch_js_top_level(self, node: Any, src: bytes, parsed: ParsedFile, mod: str) -> None:
        t = _kind(node)
        if t == "import_statement":
            parsed.imports.extend(self._js_import(node, src))
        elif t == "function_declaration":
            fn = self._js_function_declaration(node, src, mod, parent_class=None)
            parsed.functions.append(fn)
            parsed.symbols.append(_symbol(fn.name, "function", fn.start_line, fn.qualified_name))
        elif t == "class_declaration":
            cls = self._js_class(node, src, mod)
            parsed.classes.append(cls)
            parsed.symbols.append(_symbol(cls.name, "class", cls.start_line, cls.qualified_name))
        elif t in {"lexical_declaration", "variable_declaration"}:
            for declarator in _named_children(node):
                if _kind(declarator) == "variable_declarator":
                    self._js_handle_declarator(declarator, src, parsed, mod)
        elif t == "export_statement":
            # `export function foo() {...}`, `export default class X {...}`,
            # `export const Y = () => {...}`. Recurse on inner declarations so
            # they land via the same dispatch.
            for child in _named_children(node):
                self._dispatch_js_top_level(child, src, parsed, mod)
        # Deliberate skips: interface_declaration, type_alias_declaration,
        # enum_declaration, ambient_declaration. Step-1 scope cut.

    def _js_handle_declarator(
        self, declarator: Any, src: bytes, parsed: ParsedFile, mod: str
    ) -> None:
        name_node = declarator.child_by_field_name("name")
        value_node = declarator.child_by_field_name("value")
        if name_node is None or _kind(name_node) != "identifier":
            return
        name = _text(name_node, src)
        if value_node is not None and _kind(value_node) in {
            "arrow_function",
            "function_expression",
            "function",
        }:
            fn = self._js_anonymous_function(value_node, src, mod, name=name, parent_class=None)
            parsed.functions.append(fn)
            parsed.symbols.append(_symbol(name, "function", fn.start_line, fn.qualified_name))
        else:
            kind = "constant" if name.isupper() else "variable"
            parsed.symbols.append(_symbol(name, kind, _start_row(name_node), f"{mod}.{name}"))

    def _js_function_declaration(
        self,
        node: Any,
        src: bytes,
        mod: str,
        *,
        parent_class: str | None,
    ) -> ParsedFunction:
        name_node = node.child_by_field_name("name")
        name = _text(name_node, src) if name_node else "<anonymous>"
        qualified = f"{parent_class}.{name}" if parent_class else f"{mod}.{name}"
        return self._js_build_function(
            node, src, name=name, qualified=qualified, parent_class=parent_class
        )

    def _js_anonymous_function(
        self,
        node: Any,
        src: bytes,
        mod: str,
        *,
        name: str,
        parent_class: str | None,
    ) -> ParsedFunction:
        qualified = f"{parent_class}.{name}" if parent_class else f"{mod}.{name}"
        return self._js_build_function(
            node, src, name=name, qualified=qualified, parent_class=parent_class
        )

    def _js_method(self, node: Any, src: bytes, *, parent_class: str) -> ParsedFunction:
        name_node = node.child_by_field_name("name")
        name = _text(name_node, src) if name_node else "<anonymous>"
        qualified = f"{parent_class}.{name}"
        return self._js_build_function(
            node, src, name=name, qualified=qualified, parent_class=parent_class
        )

    def _js_build_function(
        self,
        node: Any,
        src: bytes,
        *,
        name: str,
        qualified: str,
        parent_class: str | None,
    ) -> ParsedFunction:
        params_node = node.child_by_field_name("parameters")
        if params_node is None:
            # arrow_function with a single bare identifier param: `x => x*2`
            params_node = node.child_by_field_name("parameter")
        if params_node is None:
            params = "()"
        else:
            params = _text(params_node, src)
            if not params.startswith("("):
                params = f"({params})"
        # TS return-type annotations land as a `type_annotation` node whose
        # text already includes the leading `:`. Empty for plain JS.
        return_type_node = node.child_by_field_name("return_type")
        return_type = _text(return_type_node, src) if return_type_node else ""
        signature = f"{params}{return_type}".strip()

        body = node.child_by_field_name("body")
        calls = self._js_calls(body, src) if body is not None else []
        is_async = any(_kind(c) == "async" for c in _children(node))

        return ParsedFunction(
            name=name,
            qualified_name=qualified,
            start_line=_start_row(node),
            end_line=_end_row(node),
            signature=signature,
            docstring=None,  # JSDoc handling deferred (step 1 scope cut)
            is_async=is_async,
            is_method=parent_class is not None,
            parent_class=parent_class,
            calls=calls,
            decorators=[],
        )

    def _js_class(self, node: Any, src: bytes, mod: str) -> ParsedClass:
        name_node = node.child_by_field_name("name")
        name = _text(name_node, src) if name_node else "<anonymous>"
        qualified = f"{mod}.{name}"

        # tree-sitter-javascript exposes `class_heritage` as a positional named
        # child (no field name); tree-sitter-typescript wraps the same data in
        # an `extends_clause`. Handle both by iterating the class's named
        # children and recognising either shape.
        base_classes: list[str] = []
        for child in _named_children(node):
            ck = _kind(child)
            if ck == "class_heritage":
                for sub in _named_children(child):
                    sub_kind = _kind(sub)
                    if sub_kind == "extends_clause":
                        for ident in _named_children(sub):
                            base_classes.append(_text(ident, src))
                    elif sub_kind == "implements_clause":
                        continue
                    else:
                        base_classes.append(_text(sub, src))

        body = node.child_by_field_name("body")
        methods: list[ParsedFunction] = []
        if body is not None:
            for child in _named_children(body):
                if _kind(child) == "method_definition":
                    methods.append(self._js_method(child, src, parent_class=qualified))

        return ParsedClass(
            name=name,
            qualified_name=qualified,
            start_line=_start_row(node),
            end_line=_end_row(node),
            docstring=None,
            base_classes=base_classes,
            methods=methods,
            decorators=[],
        )

    def _js_import(self, node: Any, src: bytes) -> list[ParsedImport]:
        """Handles ES module forms:
        `import x from 'y'`, `import { a, b as c } from 'd'`,
        `import * as ns from 'e'`, `import 'side-effect-only'`,
        and combinations such as `import def, { a } from 'm'`.
        """
        line = _start_row(node)
        source_node = node.child_by_field_name("source")
        if source_node is None:
            return []
        module_text = _strip_js_string_quotes(_text(source_node, src))

        names: list[str] = []
        alias_of: dict[str, str] = {}

        for clause in _named_children(node):
            if _kind(clause) != "import_clause":
                continue
            for child in _named_children(clause):
                ck = _kind(child)
                if ck == "identifier":
                    # default import: `import X from "m"` — record as alias of "default"
                    name = _text(child, src)
                    names.append(name)
                    alias_of[name] = "default"
                elif ck == "named_imports":
                    for spec in _named_children(child):
                        if _kind(spec) != "import_specifier":
                            continue
                        spec_name_node = spec.child_by_field_name("name")
                        spec_alias_node = spec.child_by_field_name("alias")
                        if spec_name_node is None:
                            continue
                        original = _text(spec_name_node, src)
                        names.append(original)
                        if spec_alias_node is not None:
                            alias_of[_text(spec_alias_node, src)] = original
                elif ck == "namespace_import":
                    # `import * as ns from "m"` — record under the namespace alias.
                    for sub in _named_children(child):
                        if _kind(sub) == "identifier":
                            names.append("*")
                            alias_of[_text(sub, src)] = "*"

        return [
            ParsedImport(
                module=module_text,
                names=names,
                alias_of=alias_of,
                line=line,
                is_relative=module_text.startswith(".") or module_text.startswith("/"),
            )
        ]

    def _js_calls(self, body: Any, src: bytes) -> list[ParsedCall]:
        out: list[ParsedCall] = []
        for node in _walk(body):
            if _kind(node) != "call_expression":
                continue
            fn_node = node.child_by_field_name("function")
            if fn_node is None:
                continue
            out.append(ParsedCall(callee=_text(fn_node, src), line=_start_row(node)))
        return out


    # --------------------------------- generic structural extractor (9 langs)

    def _parse_generic(
        self,
        root: Any,
        src: bytes,
        parsed: ParsedFile,
        mod: str,
        cfg: dict[str, Any],
    ) -> None:
        """Coarse but consistent extraction for languages whose grammars share
        the same conceptual shape: functions, classes (or class-like nodes
        such as struct/trait/protocol), imports, and call expressions. The
        per-language `cfg` dict pins the exact node-type strings to look for;
        see `_GENERIC_LANG_CONFIG`.

        Limitations accepted by design:
          - No docstring / decorator extraction (each language has its own
            convention; out of v1 scope).
          - Methods of classes are emitted as standalone functions whose
            `parent_class` is the enclosing class's qualified name — we do
            *not* re-emit them as `ParsedFunction` inside `ParsedClass.methods`
            for the generic case, to keep the extractor simple. The graph
            builder already deduplicates by qualified name.
          - Ruby `require` is recognised; other dynamic-import idioms (PHP's
            `include`/`require`, C++ templates, Rust macros) are skipped.
        """
        function_kinds: set[str] = set(cfg["function_kinds"])
        class_kinds: set[str] = set(cfg["class_kinds"])
        import_kinds: set[str] = set(cfg["import_kinds"])
        call_kinds: set[str] = set(cfg["call_kinds"])
        name_field: str = cfg.get("name_field", "name")
        class_name_field: str = cfg.get("class_name_field", name_field)
        callee_field: str = cfg.get("function_callee_field", "function")
        import_callee_names: set[str] = set(cfg.get("import_callee_names", []))
        is_ruby_require_call_form = self.language is SourceLanguage.ruby

        # Track which class we are currently inside while walking, so the
        # method's `parent_class` resolves correctly. We do this with a
        # manual DFS that maintains a stack rather than mutating shared state.
        stack: list[tuple[Any, str | None]] = [(root, None)]
        emitted_imports_for: set[int] = set()  # byte offsets, dedup
        emitted_functions_for: set[int] = set()
        emitted_classes_for: set[int] = set()

        while stack:
            node, parent_qname = stack.pop()
            kind = _kind(node)

            if kind in class_kinds:
                cls_qname = self._generic_class(
                    node, src, parsed, mod, class_name_field
                )
                if cls_qname is not None:
                    emitted_classes_for.add(node.start_byte())
                # descend into the class body with parent context updated
                for child in _named_children(node):
                    stack.append((child, cls_qname or parent_qname))
                continue

            if kind in function_kinds:
                if node.start_byte() not in emitted_functions_for:
                    self._generic_function(
                        node, src, parsed, mod, parent_qname, name_field
                    )
                    emitted_functions_for.add(node.start_byte())
                # don't recurse into function bodies for *more* functions;
                # nested local functions are uncommon enough that we accept
                # the gap. But still collect calls inside.
                continue

            if kind in import_kinds:
                if node.start_byte() in emitted_imports_for:
                    continue
                if is_ruby_require_call_form:
                    # ruby parses `require 'x'` as a call; only treat it as
                    # an import if the callee name is in the allowlist.
                    callee = node.child_by_field_name("method")
                    callee_text = _text(callee, src) if callee else ""
                    if callee_text not in import_callee_names:
                        # still a normal call — descend so it lands in calls
                        for child in _named_children(node):
                            stack.append((child, parent_qname))
                        continue
                imports = self._generic_imports(node, src, cfg)
                if imports:
                    parsed.imports.extend(imports)
                    emitted_imports_for.add(node.start_byte())
                continue

            if kind in call_kinds and parent_qname is not None:
                # Attach the call to the most recently emitted function.
                # DFS order makes this usually correct for top-level
                # functions; the graph builder later relinks via the
                # qualified-name index.
                fn = node.child_by_field_name(callee_field)
                if fn is not None and parsed.functions:
                    parsed.functions[-1].calls.append(
                        ParsedCall(callee=_text(fn, src), line=_start_row(node))
                    )

            for child in _named_children(node):
                stack.append((child, parent_qname))

    def _generic_function(
        self,
        node: Any,
        src: bytes,
        parsed: ParsedFile,
        mod: str,
        parent_qname: str | None,
        name_field: str,
    ) -> None:
        name = self._extract_declarator_name(node, src, name_field)
        if not name:
            name = "<anonymous>"
        qualified = f"{parent_qname}.{name}" if parent_qname else f"{mod}.{name}"
        params_node = node.child_by_field_name("parameters") or node.child_by_field_name(
            "parameter_list"
        )
        params = _text(params_node, src) if params_node else "()"
        fn = ParsedFunction(
            name=name,
            qualified_name=qualified,
            start_line=_start_row(node),
            end_line=_end_row(node),
            signature=params,
            docstring=None,
            is_async=any(_kind(c) == "async" for c in _children(node)),
            is_method=parent_qname is not None,
            parent_class=parent_qname,
            calls=[],
            decorators=[],
        )
        parsed.functions.append(fn)
        parsed.symbols.append(_symbol(name, "function", fn.start_line, qualified))

    def _generic_class(
        self,
        node: Any,
        src: bytes,
        parsed: ParsedFile,
        mod: str,
        name_field: str,
    ) -> str | None:
        name = self._extract_declarator_name(node, src, name_field)
        if not name:
            return None
        qualified = f"{mod}.{name}"
        cls = ParsedClass(
            name=name,
            qualified_name=qualified,
            start_line=_start_row(node),
            end_line=_end_row(node),
            docstring=None,
            base_classes=[],
            methods=[],
            decorators=[],
        )
        parsed.classes.append(cls)
        parsed.symbols.append(_symbol(name, "class", cls.start_line, qualified))
        return qualified

    def _extract_declarator_name(self, node: Any, src: bytes, field: str) -> str | None:
        """Find the name of a function or class declaration.

        Grammars expose the name three different ways:
          1. via `child_by_field_name("name")` (Python, JS family, Java, Swift)
          2. via `child_by_field_name("declarator")` -> nested identifier
             (C / C++ function definitions)
          3. as an unnamed `type_identifier` / `simple_identifier` child of
             the declaration node itself (Kotlin, sometimes Rust)

        We try them in that order; the first match wins.
        """
        name_node = node.child_by_field_name(field)
        if name_node is not None:
            kind = _kind(name_node)
            if kind in {"identifier", "type_identifier", "simple_identifier", "constant"}:
                return _text(name_node, src)
            ident = _find_first_identifier(name_node)
            if ident is not None:
                return _text(ident, src)
            return _text(name_node, src)

        decl = node.child_by_field_name("declarator")
        if decl is not None:
            ident = _find_first_identifier(decl)
            if ident is not None:
                return _text(ident, src)

        # last resort — first identifier-like leaf among the declaration's
        # immediate named children (skips body for class declarations because
        # `_find_first_identifier` is a DFS but the type_identifier appears
        # before the class_body in the AST).
        for child in _named_children(node):
            ck = _kind(child)
            if ck in {"identifier", "type_identifier", "simple_identifier", "constant"}:
                return _text(child, src)
        return None

    def _generic_imports(
        self, node: Any, src: bytes, cfg: dict[str, Any]
    ) -> list[ParsedImport]:
        """Extract import / use / require / include directives. Each language's
        node shape is different; we collect a best-effort module string."""
        line = _start_row(node)
        if self.language is SourceLanguage.go:
            # `import_declaration` -> `import_spec_list` or single `import_spec`
            out: list[ParsedImport] = []
            for spec in _walk(node):
                if _kind(spec) == "import_spec":
                    path = spec.child_by_field_name("path") or spec.child_by_field_name("name")
                    if path is not None:
                        out.append(
                            ParsedImport(
                                module=_strip_js_string_quotes(_text(path, src)),
                                names=[],
                                line=_start_row(spec),
                                is_relative=False,
                            )
                        )
            return out
        if self.language is SourceLanguage.ruby:
            arg = None
            for child in _named_children(node):
                if _kind(child) == "argument_list":
                    arg = child
                    break
            if arg is None:
                return []
            for child in _named_children(arg):
                if _kind(child) == "string":
                    return [
                        ParsedImport(
                            module=_strip_ruby_string(_text(child, src)),
                            names=[],
                            line=line,
                            is_relative=False,
                        )
                    ]
            return []
        if self.language is SourceLanguage.rust:
            ident = _find_first_identifier(node)
            mod = _text(node, src).removeprefix("use").strip().rstrip(";").strip()
            return [
                ParsedImport(
                    module=mod or (_text(ident, src) if ident else ""),
                    names=[],
                    line=line,
                    is_relative=False,
                )
            ]
        if self.language in {SourceLanguage.c, SourceLanguage.cpp}:
            path = node.child_by_field_name("path")
            if path is None:
                return []
            return [
                ParsedImport(
                    module=_strip_angle_or_quote(_text(path, src)),
                    names=[],
                    line=line,
                    is_relative=_text(path, src).startswith('"'),
                )
            ]
        if self.language is SourceLanguage.java:
            path = _text(node, src).removeprefix("import").rstrip(";").strip()
            return [
                ParsedImport(module=path, names=[], line=line, is_relative=False)
            ]
        if self.language is SourceLanguage.php:
            path = _text(node, src).removeprefix("use").rstrip(";").strip()
            return [
                ParsedImport(module=path, names=[], line=line, is_relative=False)
            ]
        if self.language is SourceLanguage.swift:
            path = _text(node, src).removeprefix("import").strip()
            return [
                ParsedImport(module=path, names=[], line=line, is_relative=False)
            ]
        if self.language is SourceLanguage.kotlin:
            path = _text(node, src).removeprefix("import").strip()
            return [
                ParsedImport(module=path, names=[], line=line, is_relative=False)
            ]
        return []


# ---------------------------------------------------------------------------
# node / source shim — bridges tree-sitter-language-pack's method-based API
# ---------------------------------------------------------------------------


def _kind(node: Any) -> str:
    return node.kind()


def _start_row(node: Any) -> int:
    """1-indexed source line of the node's start."""
    return node.start_position().row + 1


def _end_row(node: Any) -> int:
    return node.end_position().row + 1


def _has_error(node: Any) -> bool:
    err = node.has_error
    return err() if callable(err) else bool(err)


def _named_children(node: Any) -> list[Any]:
    return [node.named_child(i) for i in range(node.named_child_count())]


def _children(node: Any) -> list[Any]:
    return [node.child(i) for i in range(node.child_count())]


def _text(node: Any, src: bytes) -> str:
    return src[node.start_byte() : node.end_byte()].decode("utf-8", errors="replace")


def _walk(node: Any):
    """Pre-order traversal including the node itself."""
    if node is None:
        return
    yield node
    for child in _named_children(node):
        yield from _walk(child)


def _symbol(name: str, kind: str, line: int, qualified: str) -> ParsedSymbol:
    return ParsedSymbol(name=name, kind=kind, line=line, qualified_name=qualified)


_STRIPPABLE_SUFFIXES: tuple[str, ...] = (
    ".tsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".js",
    ".py",
    ".go",
    ".rb",
    ".java",
    ".rs",
    ".cpp",
    ".cxx",
    ".cc",
    ".hpp",
    ".hxx",
    ".hh",
    ".c",
    ".h",
    ".php",
    ".swift",
    ".kts",
    ".kt",
)


def _stem(path: str) -> str:
    p = path.rsplit("/", 1)[-1]
    for ext in _STRIPPABLE_SUFFIXES:
        if p.endswith(ext):
            p = p[: -len(ext)]
            break
    return p or "module"


def _strip_js_string_quotes(s: str) -> str:
    """Strip the surrounding quotes from a JS / TS module-source literal."""
    if len(s) >= 2 and s[0] in {"'", '"', "`"} and s[-1] == s[0]:
        return s[1:-1]
    return s


def _strip_ruby_string(s: str) -> str:
    """Strip surrounding quotes from a Ruby string literal as rendered by tree-sitter.
    Tree-sitter's `string` node text already includes both quote characters."""
    if len(s) >= 2 and s[0] in {"'", '"'} and s[-1] == s[0]:
        return s[1:-1]
    return s


def _strip_angle_or_quote(s: str) -> str:
    """For C/C++ `#include <foo.h>` or `#include "foo.h"` paths."""
    s = s.strip()
    if s.startswith("<") and s.endswith(">"):
        return s[1:-1]
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s


def _find_first_identifier(node: Any) -> Any | None:
    """Find the first identifier-like leaf under a node. Used for C-family
    function declarators where the function name is buried under several
    `function_declarator` / `pointer_declarator` wrappers."""
    target_kinds = {"identifier", "type_identifier", "simple_identifier", "field_identifier"}
    for sub in _walk(node):
        if _kind(sub) in target_kinds:
            return sub
    return None


def _strip_python_string_quotes(s: str) -> str:
    """Remove surrounding quotes / prefixes from a Python string literal as
    rendered by tree-sitter."""
    if not s:
        return s
    i = 0
    while i < len(s) and s[i].lower() in {"r", "b", "u", "f"}:
        i += 1
    body = s[i:]
    for triple in ('"""', "'''"):
        if body.startswith(triple) and body.endswith(triple) and len(body) >= 6:
            return body[3:-3]
    if len(body) >= 2 and body[0] in {"'", '"'} and body[-1] == body[0]:
        return body[1:-1]
    return s
