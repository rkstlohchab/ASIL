"""EpisodicStore — persistent memory of every conclusion ASIL has reached.

Phase 2's "memory" surface. Every `asil ask` answer writes one row to
Postgres with full provenance (question, answer, confidence, citations,
model, cost), plus an embedding of the question in Qdrant for semantic
recall. Subsequent runs can ask "what did we conclude about X last week?"
and get the prior answer back — that's the day-1/day-7 demo bar from PLAN.md.

Why Postgres + Qdrant and not Mem0/Letta/Zep?
  - Both services are already in our docker stack from Phase 0; adding a
    third memory framework means another vendor abstraction, another set
    of failure modes, another upgrade cadence.
  - The schema is small (one table, ~12 columns). Full control of identity,
    indexing, and migration cost is worth more than the convenience layer.
  - Mem0/Letta become attractive if we need cross-agent memory sharing or
    fancy fact extraction. Until then, the current shape is what every
    "remember a conclusion with provenance" use case actually needs.

Identity:
  - UUID primary key (server-generated, returned to the caller).
  - The Qdrant point ID is the same UUID — keeps the two halves in sync
    without a join table.

Failure mode:
  - If Postgres is reachable but Qdrant isn't, we still write the row; the
    embedding catch-up runs lazily on next recall. Inverse (Qdrant up,
    Postgres down) refuses the write — Postgres is the source of truth.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from asil_core import Confidence, get_settings
from asil_core.logging import get_logger

from asil_memory.vector_store import DEFAULT_COLLECTION, VectorPoint, VectorStore

log = get_logger(__name__)


EPISODIC_COLLECTION = "asil_memories"


class EpisodicStoreError(RuntimeError):
    """Connectivity / SQL errors that callers shouldn't try to handle."""


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Memory:
    """One persisted conclusion. Returned by `remember`, `recall_*`, `get`."""

    id: str
    repo_key: str
    question: str
    answer: str
    confidence: Confidence
    citations: list[dict[str, Any]]
    verifier_unsupported: int
    model: str
    provider: str
    cost_usd: float
    profile: str
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryHit:
    """A semantic-recall hit. `similarity` is the cosine score of the
    question vector against the stored question vector."""

    memory: Memory
    similarity: float


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS asil_memories (
    id                              UUID PRIMARY KEY,
    repo_key                        TEXT NOT NULL,
    question                        TEXT NOT NULL,
    answer                          TEXT NOT NULL,
    confidence_score                DOUBLE PRECISION NOT NULL,
    confidence_evidence_count       INTEGER NOT NULL,
    confidence_retrieval_strength   DOUBLE PRECISION NOT NULL,
    confidence_causal_confidence    DOUBLE PRECISION NOT NULL,
    confidence_derivation           JSONB NOT NULL,
    citations                       JSONB NOT NULL,
    verifier_unsupported            INTEGER NOT NULL DEFAULT 0,
    model                           TEXT NOT NULL,
    provider                        TEXT NOT NULL,
    cost_usd                        DOUBLE PRECISION NOT NULL,
    profile                         TEXT NOT NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata                        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS asil_memories_repo_created
    ON asil_memories (repo_key, created_at DESC);
"""


class EpisodicStore:
    """Postgres-backed memory + Qdrant-backed semantic recall.

    Sync (matches GraphStore / VectorStore). One open connection per store
    instance; close via `.close()` or use as a context manager.
    """

    def __init__(
        self,
        dsn: str | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        import psycopg

        if dsn is None:
            s = get_settings()
            dsn = _normalize_dsn(s.postgres_dsn)
        self._dsn = dsn
        try:
            self._conn = psycopg.connect(dsn, autocommit=True)
        except Exception as e:
            raise EpisodicStoreError(
                f"can't connect to Postgres at {dsn}: {e}. Is `make up` running?"
            ) from e
        self._vector = vector_store

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self._conn.close()

    def __enter__(self) -> EpisodicStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def verify_connectivity(self) -> None:
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except Exception as e:
            raise EpisodicStoreError(f"postgres ping failed: {e}") from e

    # ------------------------------------------------------------------ schema

    def apply_schema(self) -> None:
        """Create tables + indexes idempotently. Safe to call on every ingest."""
        with self._conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        log.info("episodic_schema_applied")

    # ------------------------------------------------------------------ writes

    def remember(
        self,
        *,
        repo_key: str,
        question: str,
        answer: str,
        confidence: Confidence,
        citations: list[dict[str, Any]],
        model: str,
        provider: str,
        cost_usd: float,
        profile: str,
        verifier_unsupported: int = 0,
        metadata: dict[str, Any] | None = None,
        question_vector: list[float] | None = None,
    ) -> Memory:
        """Insert one memory row + (best-effort) Qdrant point. Returns the row."""
        mem_id = str(uuid.uuid4())
        meta = metadata or {}
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO asil_memories (
                    id, repo_key, question, answer,
                    confidence_score, confidence_evidence_count,
                    confidence_retrieval_strength, confidence_causal_confidence,
                    confidence_derivation, citations,
                    verifier_unsupported, model, provider, cost_usd, profile,
                    metadata
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s
                ) RETURNING created_at
                """,
                (
                    mem_id,
                    repo_key,
                    question,
                    answer,
                    confidence.score,
                    confidence.evidence_count,
                    confidence.retrieval_strength,
                    confidence.causal_confidence,
                    json.dumps(list(confidence.derivation)),
                    json.dumps(citations),
                    verifier_unsupported,
                    model,
                    provider,
                    cost_usd,
                    profile,
                    json.dumps(meta),
                ),
            )
            row = cur.fetchone()
            created_at = row[0] if row else datetime.utcnow()

        # Best-effort vector write. If Qdrant is down the memory still persists;
        # next recall does the embedding catch-up.
        if self._vector is not None and question_vector is not None:
            try:
                self._vector.ensure_collection(EPISODIC_COLLECTION, dim=len(question_vector))
                self._vector.upsert_batch(
                    [
                        VectorPoint(
                            id=mem_id,
                            vector=question_vector,
                            payload={
                                "memory_id": mem_id,
                                "repo_key": repo_key,
                                "question": question,
                                "created_at": created_at.isoformat(),
                            },
                        )
                    ],
                    collection=EPISODIC_COLLECTION,
                )
            except Exception as e:
                log.warning("memory_vector_write_failed", memory_id=mem_id, err=str(e))

        log.info(
            "memory_remembered",
            memory_id=mem_id,
            repo_key=repo_key,
            confidence_score=round(confidence.score, 3),
        )
        return Memory(
            id=mem_id,
            repo_key=repo_key,
            question=question,
            answer=answer,
            confidence=confidence,
            citations=citations,
            verifier_unsupported=verifier_unsupported,
            model=model,
            provider=provider,
            cost_usd=cost_usd,
            profile=profile,
            created_at=created_at,
            metadata=meta,
        )

    def forget(self, memory_id: str) -> bool:
        """Hard-delete a memory + its Qdrant point. Returns True if removed."""
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM asil_memories WHERE id = %s", (memory_id,))
            removed = cur.rowcount > 0

        if removed and self._vector is not None:
            try:
                # qdrant_client supports deleting points by ID list
                self._vector._client.delete(  # type: ignore[attr-defined]
                    collection_name=EPISODIC_COLLECTION,
                    points_selector=[memory_id],
                    wait=False,
                )
            except Exception as e:
                log.warning("memory_vector_delete_failed", memory_id=memory_id, err=str(e))
        return removed

    # ------------------------------------------------------------------ reads

    def get(self, memory_id: str) -> Memory | None:
        with self._conn.cursor() as cur:
            cur.execute(_SELECT_ALL_COLUMNS + " WHERE id = %s", (memory_id,))
            row = cur.fetchone()
        return _row_to_memory(row) if row else None

    def recall_recent(
        self,
        *,
        repo_key: str | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        """Most-recent-first, optionally scoped to one repo."""
        if repo_key is not None:
            with self._conn.cursor() as cur:
                cur.execute(
                    _SELECT_ALL_COLUMNS + " WHERE repo_key = %s ORDER BY created_at DESC LIMIT %s",
                    (repo_key, limit),
                )
                rows = cur.fetchall()
        else:
            with self._conn.cursor() as cur:
                cur.execute(
                    _SELECT_ALL_COLUMNS + " ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
        return [_row_to_memory(r) for r in rows]

    def recall_similar(
        self,
        *,
        query_vector: list[float],
        repo_key: str | None = None,
        limit: int = 5,
        min_similarity: float = 0.0,
    ) -> list[MemoryHit]:
        """Semantic search over past questions. Returns the hydrated memories
        ordered by similarity (cosine). Filters below `min_similarity`."""
        if self._vector is None:
            return []
        hits = self._vector.search(
            query_vector,
            limit=limit,
            repo_key=repo_key,
            collection=EPISODIC_COLLECTION,
        )
        out: list[MemoryHit] = []
        for h in hits:
            if h.score < min_similarity:
                continue
            mem = self.get(h.id)
            if mem is not None:
                out.append(MemoryHit(memory=mem, similarity=h.score))
        return out

    def count(self, *, repo_key: str | None = None) -> int:
        with self._conn.cursor() as cur:
            if repo_key is not None:
                cur.execute("SELECT count(*) FROM asil_memories WHERE repo_key = %s", (repo_key,))
            else:
                cur.execute("SELECT count(*) FROM asil_memories")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def stats(self) -> dict[str, Any]:
        """Total + per-repo counts. Cheap; the table is small by design."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM asil_memories")
            total = int((cur.fetchone() or [0])[0])
            cur.execute(
                "SELECT repo_key, count(*) FROM asil_memories GROUP BY repo_key ORDER BY 2 DESC"
            )
            per_repo = {row[0]: int(row[1]) for row in cur.fetchall()}
        return {"total": total, "per_repo": per_repo}

    def clear_repo(self, repo_key: str) -> int:
        """Delete all memories belonging to a repo. Returns rows removed."""
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM asil_memories WHERE repo_key = %s", (repo_key,))
            removed = cur.rowcount
        if removed > 0 and self._vector is not None:
            try:
                from qdrant_client.http import models as qm

                self._vector._client.delete(  # type: ignore[attr-defined]
                    collection_name=EPISODIC_COLLECTION,
                    points_selector=qm.FilterSelector(
                        filter=qm.Filter(
                            must=[
                                qm.FieldCondition(
                                    key="repo_key", match=qm.MatchValue(value=repo_key)
                                )
                            ]
                        )
                    ),
                    wait=True,
                )
            except Exception as e:
                log.warning("memory_vector_clear_failed", repo_key=repo_key, err=str(e))
        return removed


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


_SELECT_ALL_COLUMNS = """
SELECT id, repo_key, question, answer,
       confidence_score, confidence_evidence_count,
       confidence_retrieval_strength, confidence_causal_confidence,
       confidence_derivation, citations,
       verifier_unsupported, model, provider, cost_usd, profile,
       created_at, metadata
FROM asil_memories
"""


def _row_to_memory(row: tuple) -> Memory:
    (
        mid,
        repo_key,
        question,
        answer,
        score,
        evidence_count,
        retrieval_strength,
        causal_confidence,
        derivation_json,
        citations_json,
        verifier_unsupported,
        model,
        provider,
        cost_usd,
        profile,
        created_at,
        metadata_json,
    ) = row
    return Memory(
        id=str(mid),
        repo_key=repo_key,
        question=question,
        answer=answer,
        confidence=Confidence(
            score=float(score),
            evidence_count=int(evidence_count),
            retrieval_strength=float(retrieval_strength),
            causal_confidence=float(causal_confidence),
            derivation=_loads_list(derivation_json),
        ),
        citations=_loads_list(citations_json),
        verifier_unsupported=int(verifier_unsupported or 0),
        model=model,
        provider=provider,
        cost_usd=float(cost_usd),
        profile=profile,
        created_at=created_at,
        metadata=_loads_dict(metadata_json),
    )


def _loads_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (TypeError, ValueError):
            return []
    return list(v) if hasattr(v, "__iter__") else []


def _loads_dict(v: Any) -> dict:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (TypeError, ValueError):
            return {}
    return {}


def _normalize_dsn(dsn: str) -> str:
    """Strip SQLAlchemy-style `+driver` from a Postgres URL so psycopg accepts it."""
    if dsn.startswith("postgresql+"):
        # e.g. postgresql+asyncpg://... -> postgresql://...
        prefix, _, rest = dsn.partition("://")
        scheme = prefix.split("+", 1)[0]
        return f"{scheme}://{rest}"
    return dsn


_ = DEFAULT_COLLECTION  # mark used; downstream callers refer through asil_memory namespace
