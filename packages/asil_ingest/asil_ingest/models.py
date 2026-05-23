"""Typed AST representation produced by `TreeSitterParser`.

These models are the contract between parsing and graph-building:
  - `treesitter_parser.parse_source(src, lang) -> ParsedFile`
  - `graph_builder.write(parsed: ParsedFile)` (Phase 1 milestone 1.2)
  - chunkers / embedders consume the same ParsedFile to keep chunk identity
    aligned with graph node identity.

All line numbers are 1-indexed (matching IDE / git blame conventions).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class SourceLanguage(StrEnum):
    python = "python"
    typescript = "typescript"
    javascript = "javascript"
    tsx = "tsx"
    go = "go"


class ParsedImport(BaseModel):
    """A single import statement.

    For `from foo.bar import baz, qux as q`:
      module = "foo.bar"
      names = ["baz", "qux"]
      alias_of = {"q": "qux"}
    For `import foo.bar`:
      module = "foo.bar"
      names = []
    """

    module: str
    names: list[str] = Field(default_factory=list)
    alias_of: dict[str, str] = Field(default_factory=dict)
    line: int
    is_relative: bool = False  # `from .foo import bar` → True


class ParsedCall(BaseModel):
    """A call site inside a function/method.

    `callee` is the textual reference as written in source. Resolving it to a
    fully-qualified symbol is the job of the SCIP layer (Phase 1 milestone 1.7).
    """

    callee: str
    line: int


class ParsedSymbol(BaseModel):
    """A name *defined* in this file (function, class, top-level variable).
    Stored separately from functions/classes for cheap symbol lookups.
    """

    name: str
    kind: str  # "function" | "class" | "method" | "variable" | "constant"
    line: int
    qualified_name: str  # "module.Class.method" — populated by parser


class ParsedFunction(BaseModel):
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    signature: str  # "(a: int, b: str = 'x') -> bool"
    docstring: str | None = None
    is_async: bool = False
    is_method: bool = False
    parent_class: str | None = None  # qualified name of enclosing class
    calls: list[ParsedCall] = Field(default_factory=list)
    decorators: list[str] = Field(default_factory=list)


class ParsedClass(BaseModel):
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    docstring: str | None = None
    base_classes: list[str] = Field(default_factory=list)
    methods: list[ParsedFunction] = Field(default_factory=list)
    decorators: list[str] = Field(default_factory=list)


class ParsedFile(BaseModel):
    """The full structural view of one source file.

    `path` is the file's path **relative to the repo root**. The repo identity
    lives on a separate `Repo` graph node; combining them gives a globally
    unique file identity.
    """

    path: str
    language: SourceLanguage
    module_name: str | None = None  # "asil_core.llm.router" for python; None for langs w/o modules
    imports: list[ParsedImport] = Field(default_factory=list)
    functions: list[ParsedFunction] = Field(default_factory=list)
    classes: list[ParsedClass] = Field(default_factory=list)
    symbols: list[ParsedSymbol] = Field(default_factory=list)
    loc: int = 0  # raw line count of the file
    parse_errors: list[str] = Field(default_factory=list)

    def all_functions_including_methods(self) -> list[ParsedFunction]:
        out = list(self.functions)
        for c in self.classes:
            out.extend(c.methods)
        return out
