"""Hybrid retriever — the unified read path.

Combines two complementary lookups:
  - Vector search (Qdrant): fuzzy, semantic, fast on the full corpus.
  - Graph expansion (Neo4j): structural, exact, gives every hit its surrounding
    context (parent class for a method, containing file for a class, etc.).

Workflow:
  1. Embed the natural-language query via ModelRouter.embed() once.
  2. Top-K vector search → candidate set (cheap fuzzy filter).
  3. For each candidate, fetch its 1-hop graph neighborhood (parent + siblings
     scoped to the same file) and merge that into the candidate set.
  4. De-dupe on qualified_name; preserve the best vector score we saw.
  5. Return ranked candidates with full provenance.

We do NOT re-rank with an LLM here. That belongs in the reasoning pipeline
(`asil ask`), where the retriever's output is one input among others
(confidence, prior conclusions, etc.). Phase 1.5's contract is "give the
reasoner the right candidates"; Phase 2's verifier closes the loop.

This module is intentionally store-shape-agnostic: GraphStore and VectorStore
are passed in, never constructed here. That makes the retriever trivially
mockable in unit tests and lets later phases plug in different backends
(e.g., the Phase 4 temporal store) without touching this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from asil_core import Confidence
from asil_core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RetrievalCandidate:
    """One node returned by the retriever.

    The fields chosen are exactly what a downstream reasoner needs to cite
    a claim: where the symbol lives (file:line), what kind it is, what name
    to refer to it by, and the actual snippet text it was embedded as.
    """

    qualified_name: str
    name: str
    kind: str  # "function" | "class"
    file_path: str
    start_line: int
    end_line: int
    score: float  # primary signal: cosine sim from the vector store
    source: str  # "vector" | "graph_expand"
    parent_class: str | None = None
    signature: str | None = None
    docstring: str | None = None
    text: str = ""  # what we embedded — useful for prompt-building
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RetrievalResult:
    query: str
    candidates: list[RetrievalCandidate]
    confidence: Confidence

    def top(self, n: int) -> list[RetrievalCandidate]:
        return self.candidates[:n]


class _Embedder(Protocol):
    """Minimal contract the retriever needs. ModelRouter satisfies this."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class HybridRetriever:
    """Vector → graph-expand → dedupe → rank.

    Configurable knobs:
      - `vector_top_k`: how many vector candidates to seed with (default 20).
      - `final_limit`: how many results to surface (default 10).
      - `expand_hops`: how far to walk the graph from each vector hit
        (default 1; 2 is useful for "who calls this" once SCIP lands).
    """

    def __init__(
        self,
        graph_store: Any,  # asil_memory.GraphStore — Any to avoid import cycle
        vector_store: Any,  # asil_memory.VectorStore
        embedder: _Embedder,
        *,
        vector_top_k: int = 20,
        final_limit: int = 10,
        expand_hops: int = 1,
    ) -> None:
        self._graph = graph_store
        self._vstore = vector_store
        self._embedder = embedder
        self._vector_top_k = vector_top_k
        self._final_limit = final_limit
        self._expand_hops = expand_hops

    async def retrieve(
        self,
        query: str,
        *,
        repo_key: str | None = None,
        kind: str | None = None,
    ) -> RetrievalResult:
        # 1. Embed once.
        query_vec = (await self._embedder.embed([query]))[0]

        # 2. Vector top-K.
        hits = self._vstore.search(
            query_vec,
            limit=self._vector_top_k,
            repo_key=repo_key,
            kind=kind,
        )
        log.debug("retrieve_vector_hits", n=len(hits), query=query[:80])

        seen: dict[str, RetrievalCandidate] = {}
        for h in hits:
            c = _candidate_from_vector_hit(h)
            seen[c.qualified_name] = c

        # 3. Graph expand each vector hit. Bounded by `expand_hops`. We pull
        #    parents (containing class / file) and siblings *in the same
        #    parent* — enough surrounding context without exploding the set.
        #    Skip expansion when the vector pool already saturates the budget;
        #    no reason to spam Neo4j.
        if seen and self._expand_hops > 0 and len(seen) < self._final_limit * 3:
            qnames = list(seen.keys())
            neighbor_rows = self._graph_neighbors(qnames, repo_key=repo_key)
            for row in neighbor_rows:
                qn = row["qualified_name"]
                if qn in seen:
                    continue
                seen[qn] = _candidate_from_graph_row(row)

        # 4. Rank. Vector score is the primary signal; graph-expanded nodes
        #    inherit a small fixed score (lower than the worst vector hit)
        #    so they don't displace strong vector matches but still surface
        #    when the user wants to follow context.
        worst_vec_score = min((c.score for c in seen.values() if c.source == "vector"), default=0.0)
        for c in seen.values():
            if c.source == "graph_expand" and c.score == 0.0:
                c.score = max(0.0, worst_vec_score * 0.5)

        ranked = sorted(seen.values(), key=lambda c: c.score, reverse=True)
        top = ranked[: self._final_limit]

        confidence = _retrieval_confidence(top, hits)
        return RetrievalResult(query=query, candidates=top, confidence=confidence)

    # ------------------------------------------------------------------ graph

    def _graph_neighbors(
        self, qualified_names: list[str], *, repo_key: str | None
    ) -> list[dict[str, Any]]:
        """For each qname, return the parent + sibling nodes in its container.

        Specifically:
          - For a Function that's a method: its Class siblings (other methods)
            and the Class itself, plus the File.
          - For a top-level Function: other top-level Functions in the same File
            plus the File.
          - For a Class: its methods + the File.

        We cap the per-query result to keep the candidate set tractable.
        """
        if not qualified_names:
            return []

        # One Cypher per call — bounded by len(qualified_names) which is bounded
        # by `vector_top_k`. Phase 2 may switch to UNWIND once we feel cost.
        params = {"qnames": qualified_names}
        if repo_key is not None:
            params["repo_key"] = repo_key

        repo_clause = "AND n.repo_key = $repo_key" if repo_key else ""
        cypher = f"""
        UNWIND $qnames AS qn
        MATCH (n {{qualified_name: qn}})
        WHERE (n:Function OR n:Class) {repo_clause}

        // walk up to the immediate container, then back down to siblings
        OPTIONAL MATCH (parent)-[:CONTAINS]->(n)
        WHERE parent:Class OR parent:File
        OPTIONAL MATCH (parent)-[:CONTAINS]->(sib)
        WHERE (sib:Function OR sib:Class) AND sib.qualified_name <> qn

        WITH collect(DISTINCT parent) AS parents, collect(DISTINCT sib) AS sibs
        UNWIND parents + sibs AS nbr
        WITH DISTINCT nbr WHERE nbr IS NOT NULL
        RETURN
            labels(nbr)[0] AS label,
            nbr.qualified_name AS qualified_name,
            nbr.name AS name,
            nbr.file_path AS file_path,
            nbr.start_line AS start_line,
            nbr.end_line AS end_line,
            nbr.signature AS signature,
            nbr.docstring AS docstring,
            nbr.parent_class AS parent_class
        LIMIT 100
        """
        try:
            return self._graph.query(cypher, **params)
        except Exception as e:  # pragma: no cover
            log.warning("graph_expand_failed", err=str(e))
            return []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _candidate_from_vector_hit(hit: Any) -> RetrievalCandidate:
    p = hit.payload or {}
    return RetrievalCandidate(
        qualified_name=p.get("qualified_name", ""),
        name=p.get("name", ""),
        kind=p.get("kind", "?"),
        file_path=p.get("file_path", "?"),
        start_line=int(p.get("start_line") or 0),
        end_line=int(p.get("end_line") or 0),
        score=float(hit.score),
        source="vector",
        parent_class=p.get("parent_class"),
        signature=p.get("signature"),
        docstring=p.get("docstring"),
        text=p.get("text", ""),
    )


def _candidate_from_graph_row(row: dict[str, Any]) -> RetrievalCandidate:
    label = (row.get("label") or "").lower()
    if label == "file":
        kind = "file"
    elif label == "class":
        kind = "class"
    else:
        kind = "function"
    return RetrievalCandidate(
        qualified_name=row.get("qualified_name") or "",
        name=row.get("name") or "",
        kind=kind,
        file_path=row.get("file_path") or "?",
        start_line=int(row.get("start_line") or 0),
        end_line=int(row.get("end_line") or 0),
        score=0.0,  # filled in by the ranker
        source="graph_expand",
        parent_class=row.get("parent_class"),
        signature=row.get("signature"),
        docstring=row.get("docstring"),
        text="",
    )


def _retrieval_confidence(
    top: list[RetrievalCandidate],
    raw_hits: list[Any],
) -> Confidence:
    """Confidence in *the retrieval*, not in any downstream answer.

    Heuristic:
      - score: top-1 cosine sim (0..1), bounded.
      - evidence_count: how many vector hits we got (proxy for "the question
        is grounded in this corpus").
      - retrieval_strength: average top-3 score.
      - derivation: short human-readable trail.

    The verifier in `asil ask` will downgrade this further if it finds that
    the LLM's answer wasn't supported by the retrieved snippets.
    """
    if not top:
        return Confidence.unknown()

    top_score = max(0.0, min(1.0, top[0].score))
    top3 = [c.score for c in top[:3] if c.score > 0]
    avg_top3 = sum(top3) / len(top3) if top3 else 0.0

    derivation = [
        f"top hit: {top[0].qualified_name} (score={top[0].score:.3f})",
        f"vector candidates: {len(raw_hits)}",
        f"graph-expanded candidates: {sum(1 for c in top if c.source == 'graph_expand')}",
    ]
    return Confidence(
        score=top_score,
        evidence_count=len(raw_hits),
        retrieval_strength=avg_top3,
        causal_confidence=0.0,  # filled in by Phase 4 temporal layer
        derivation=derivation,
    )
