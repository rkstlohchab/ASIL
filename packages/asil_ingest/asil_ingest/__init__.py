"""ASIL ingest: parse source repos into a typed AST representation,
then into the Neo4j knowledge graph + Qdrant embeddings.

Phase 1 surface (this module):
  - models       : Pydantic models for parsed AST
  - treesitter_parser : per-language Tree-sitter parsers (Python first)

Phase 1 still-to-come (separate modules):
  - graph_builder : write ParsedFile → Neo4j nodes/edges
  - scip_indexer  : enrich the graph with SCIP cross-reference data
  - repo_cloner   : clone + incremental re-index
"""

from asil_ingest.models import (
    ParsedCall,
    ParsedClass,
    ParsedFile,
    ParsedFunction,
    ParsedImport,
    ParsedSymbol,
    SourceLanguage,
)
from asil_ingest.repo_cloner import (
    IGNORED_DIRS,
    LANGUAGE_EXTENSIONS,
    ResolvedRepo,
    iter_source_files,
    language_of,
    resolve_repo,
)
from asil_ingest.treesitter_parser import TreeSitterParser, parse_source

__version__ = "0.0.1"

__all__ = [
    "IGNORED_DIRS",
    "LANGUAGE_EXTENSIONS",
    "ParsedCall",
    "ParsedClass",
    "ParsedFile",
    "ParsedFunction",
    "ParsedImport",
    "ParsedSymbol",
    "ResolvedRepo",
    "SourceLanguage",
    "TreeSitterParser",
    "iter_source_files",
    "language_of",
    "parse_source",
    "resolve_repo",
]
