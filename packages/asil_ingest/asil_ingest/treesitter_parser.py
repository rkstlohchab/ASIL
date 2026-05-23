"""Tree-sitter-based parser producing typed `ParsedFile` records.

Phase 1 milestone 1.1: Python only. JS/TS/Go follow.

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


class TreeSitterParser:
    def __init__(self, language: SourceLanguage) -> None:
        self.language = language
        if language is not SourceLanguage.python:
            raise NotImplementedError(
                f"Phase 1 milestone 1.1 ships Python only; {language} arrives next."
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

        if self.language is SourceLanguage.python:
            self._parse_python(root, src_bytes, parsed, module_name or _stem(path))
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


def _stem(path: str) -> str:
    p = path.rsplit("/", 1)[-1]
    if p.endswith(".py"):
        p = p[:-3]
    return p or "module"


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
