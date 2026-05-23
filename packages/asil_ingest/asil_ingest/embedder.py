"""Chunk ParsedFile records into AST-aligned units, embed them, store in Qdrant.

Why function-level chunks (not arbitrary 500-token windows)?
  - Semantic boundaries: a function is a unit of meaning; an arbitrary slice
    can split a docstring from its function, or a class from its first method.
  - Retrieval quality: search hits map cleanly to "go look at this function"
    instead of "go look at line 247-311 of this file."
  - Graph alignment: every chunk's ID == graph node ID (qualified_name +
    repo_key). The hybrid retriever (Phase 1.5) crosses freely between
    vector hits and graph traversal because they share identity.

We embed in batches via `ModelRouter.embed(texts)` to amortize HTTP overhead
and stay within whatever rate limit the active profile's provider enforces.
Each chunk's text is name + signature + docstring + the first ~500 chars of
the source body; that's enough signal for retrieval without paying to embed
the entire file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from asil_core.logging import get_logger
from asil_memory import (
    DEFAULT_COLLECTION,
    VectorPoint,
    VectorStore,
    point_id_for,
)

from asil_ingest.models import ParsedClass, ParsedFile, ParsedFunction

log = get_logger(__name__)

# Truncate the source body included in each chunk's text. Empirically 500-800
# chars covers most function bodies; longer ones contribute diminishing return
# for embedding quality and add cost.
BODY_CHAR_BUDGET = 800

# How many chunks to embed per `router.embed()` call. Most providers accept
# hundreds; we stay conservative to keep retry blast radius small.
EMBED_BATCH_SIZE = 32


@dataclass(slots=True)
class EmbedStats:
    chunks_embedded: int = 0
    files_processed: int = 0
    failures: list[str] = field(default_factory=list)
    total_input_chars: int = 0


@dataclass(slots=True)
class _Chunk:
    repo_key: str
    qualified_name: str
    kind: str  # "function" | "class"
    file_path: str
    start_line: int
    end_line: int
    text: str  # what we actually embed
    payload: dict[str, Any]  # what we store in Qdrant


class Embedder:
    """Stateless coordinator. Pass in a router + vector_store; reuse across files.

    Lifecycle: caller calls `ensure_collection(dim)` once, then `embed_file(...)`
    per file. The router/embedder figures out the vector dim from a probe
    embedding; downstream callers can either pass `dim` explicitly or call
    `probe_dim()` once and reuse.
    """

    def __init__(
        self,
        router: Any,  # asil_core.llm.ModelRouter — typed `Any` to avoid circular import
        vector_store: VectorStore,
        *,
        repo_root: Path | None = None,
        collection: str = DEFAULT_COLLECTION,
    ) -> None:
        self._router = router
        self._vstore = vector_store
        self._repo_root = repo_root
        self._collection = collection

    async def probe_dim(self) -> int:
        """One throwaway embedding to learn the vector dim of the active embedder."""
        vec = await self._router.embed(["dim probe"])
        return len(vec[0])

    def ensure_collection(self, dim: int) -> None:
        self._vstore.ensure_collection(self._collection, dim=dim)

    async def embed_file(self, repo_key: str, parsed: ParsedFile) -> int:
        """Build chunks for `parsed`, embed them, upsert into Qdrant.

        Returns the number of chunks written.
        """
        chunks = self._chunks_for_file(repo_key, parsed)
        if not chunks:
            return 0

        # Batch embed → upsert. Each batch is one HTTP round-trip.
        total_written = 0
        for i in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[i : i + EMBED_BATCH_SIZE]
            vectors = await self._router.embed([c.text for c in batch])
            points = [
                VectorPoint(
                    id=point_id_for(c.repo_key, c.qualified_name),
                    vector=vectors[j],
                    payload=c.payload,
                )
                for j, c in enumerate(batch)
            ]
            self._vstore.upsert_batch(points, collection=self._collection)
            total_written += len(points)
        return total_written

    def _chunks_for_file(self, repo_key: str, parsed: ParsedFile) -> list[_Chunk]:
        """One chunk per top-level function, one per class, one per method."""
        chunks: list[_Chunk] = []

        # Read source once so we can quote real bodies. If the file is gone
        # (rare; race between resolver and embedder), fall back to signature-only.
        body_source = self._read_source(parsed.path) or ""

        for fn in parsed.functions:
            chunks.append(self._function_chunk(repo_key, parsed, fn, body_source))
        for cls in parsed.classes:
            chunks.append(self._class_chunk(repo_key, parsed, cls, body_source))
            for m in cls.methods:
                chunks.append(self._function_chunk(repo_key, parsed, m, body_source))
        return chunks

    def _read_source(self, file_path: str) -> str | None:
        if self._repo_root is None:
            return None
        try:
            return (self._repo_root / file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def _function_chunk(
        self, repo_key: str, parsed: ParsedFile, fn: ParsedFunction, source: str
    ) -> _Chunk:
        body = _slice_lines(source, fn.start_line, fn.end_line)[:BODY_CHAR_BUDGET]
        text_parts = [
            f"# {fn.qualified_name}{fn.signature}",
        ]
        if fn.docstring:
            text_parts.append(f"# {fn.docstring}")
        if fn.decorators:
            text_parts.append("# decorators: " + ", ".join(fn.decorators))
        if body:
            text_parts.append("")
            text_parts.append(body)
        text = "\n".join(text_parts)

        return _Chunk(
            repo_key=repo_key,
            qualified_name=fn.qualified_name,
            kind="function",
            file_path=parsed.path,
            start_line=fn.start_line,
            end_line=fn.end_line,
            text=text,
            payload={
                "repo_key": repo_key,
                "qualified_name": fn.qualified_name,
                "name": fn.name,
                "kind": "function",
                "file_path": parsed.path,
                "language": parsed.language.value,
                "start_line": fn.start_line,
                "end_line": fn.end_line,
                "signature": fn.signature,
                "docstring": fn.docstring,
                "is_async": fn.is_async,
                "is_method": fn.is_method,
                "parent_class": fn.parent_class,
                "text": text,  # mirror so search results can show snippets without re-embedding
            },
        )

    def _class_chunk(
        self, repo_key: str, parsed: ParsedFile, cls: ParsedClass, source: str
    ) -> _Chunk:
        body = _slice_lines(source, cls.start_line, cls.end_line)[:BODY_CHAR_BUDGET]
        text_parts = [f"# class {cls.qualified_name}"]
        if cls.base_classes:
            text_parts.append("# inherits: " + ", ".join(cls.base_classes))
        if cls.docstring:
            text_parts.append(f"# {cls.docstring}")
        if cls.decorators:
            text_parts.append("# decorators: " + ", ".join(cls.decorators))
        method_names = [m.name for m in cls.methods]
        if method_names:
            text_parts.append("# methods: " + ", ".join(method_names))
        if body:
            text_parts.append("")
            text_parts.append(body)
        text = "\n".join(text_parts)

        return _Chunk(
            repo_key=repo_key,
            qualified_name=cls.qualified_name,
            kind="class",
            file_path=parsed.path,
            start_line=cls.start_line,
            end_line=cls.end_line,
            text=text,
            payload={
                "repo_key": repo_key,
                "qualified_name": cls.qualified_name,
                "name": cls.name,
                "kind": "class",
                "file_path": parsed.path,
                "language": parsed.language.value,
                "start_line": cls.start_line,
                "end_line": cls.end_line,
                "docstring": cls.docstring,
                "base_classes": cls.base_classes,
                "method_names": method_names,
                "text": text,
            },
        )


def _slice_lines(source: str, start_line: int, end_line: int) -> str:
    """1-indexed inclusive line slice. Safe on short / missing source."""
    if not source:
        return ""
    lines = source.splitlines()
    s = max(0, start_line - 1)
    e = min(len(lines), end_line)
    return "\n".join(lines[s:e])
