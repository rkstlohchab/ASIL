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
from asil_core.identity import get_machine_id, get_origin_agent, get_user_id
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
    recall_hits: int = 0
    user_id: str = "unknown"
    machine_id: str = "unknown"
    origin_agent: str = "cli"
    origin_session_id: str | None = None
    team_id: str = "default"


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
    metadata                        JSONB NOT NULL DEFAULT '{}'::jsonb,
    recall_hits                     INTEGER NOT NULL DEFAULT 0,
    user_id                         TEXT NOT NULL DEFAULT 'unknown',
    machine_id                      TEXT NOT NULL DEFAULT 'unknown',
    origin_agent                    TEXT NOT NULL DEFAULT 'cli',
    origin_session_id               TEXT
);
ALTER TABLE asil_memories ADD COLUMN IF NOT EXISTS recall_hits INTEGER NOT NULL DEFAULT 0;
ALTER TABLE asil_memories ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE asil_memories ADD COLUMN IF NOT EXISTS machine_id TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE asil_memories ADD COLUMN IF NOT EXISTS origin_agent TEXT NOT NULL DEFAULT 'cli';
ALTER TABLE asil_memories ADD COLUMN IF NOT EXISTS origin_session_id TEXT;
ALTER TABLE asil_memories ADD COLUMN IF NOT EXISTS team_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS asil_memories_repo_created
    ON asil_memories (repo_key, created_at DESC);
CREATE INDEX IF NOT EXISTS asil_memories_origin_agent
    ON asil_memories (origin_agent, created_at DESC);
CREATE INDEX IF NOT EXISTS asil_memories_team_repo
    ON asil_memories (team_id, repo_key, created_at DESC);

-- Phase 9.2 — every remember() call lands one row here, whether it inserted
-- a new memory or folded into an existing one. Lets us answer "what's our
-- dedupe rate?" and "which agents produce the most folds?" from real data.
CREATE TABLE IF NOT EXISTS asil_memory_writes (
    id                    BIGSERIAL PRIMARY KEY,
    ts                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    repo_key              TEXT NOT NULL,
    user_id               TEXT NOT NULL DEFAULT 'unknown',
    origin_agent          TEXT NOT NULL DEFAULT 'cli',
    source                TEXT,
    question_text         TEXT NOT NULL,
    nearest_existing_id   UUID,
    nearest_similarity    DOUBLE PRECISION,
    outcome               TEXT NOT NULL,
    resulting_memory_id   UUID NOT NULL,
    team_id               TEXT NOT NULL DEFAULT 'default'
);
ALTER TABLE asil_memory_writes ADD COLUMN IF NOT EXISTS team_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS asil_memory_writes_ts ON asil_memory_writes (ts DESC);
CREATE INDEX IF NOT EXISTS asil_memory_writes_outcome_ts
    ON asil_memory_writes (outcome, ts DESC);
CREATE INDEX IF NOT EXISTS asil_memory_writes_repo_ts
    ON asil_memory_writes (repo_key, ts DESC);
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
        user_id: str | None = None,
        machine_id: str | None = None,
        origin_agent: str | None = None,
        origin_session_id: str | None = None,
        team_id: str = "default",
        dedupe_threshold: float | None = 0.95,
    ) -> Memory:
        """Insert one memory row + (best-effort) Qdrant point. Returns the row.

        Identity fields (`user_id` / `machine_id` / `origin_agent` /
        `origin_session_id`) default to the local environment via
        `asil_core.identity`. MCP callers pass explicit values from the
        client; the CLI lets the defaults kick in.

        Write-time dedupe (Phase 9.2): if `dedupe_threshold` is set and a
        prior memory in the same repo has cosine similarity >= threshold
        on its question vector, we **fold** instead of INSERT:
        `recall_hits` on the existing row is bumped, the new source is
        appended to `metadata.sources`, and the existing `Memory` is
        returned. The event lands in `asil_memory_writes` with
        `outcome='folded'`. Pass `dedupe_threshold=None` to force a fresh
        INSERT regardless of similarity (e.g. for the out-of-band
        `asil.remember` MCP tool).
        """
        meta = metadata or {}
        resolved_user_id = user_id if user_id is not None else get_user_id()
        resolved_machine_id = machine_id if machine_id is not None else get_machine_id()
        resolved_origin_agent = get_origin_agent(origin_agent)
        source = meta.get("source") if isinstance(meta, dict) else None

        # ----------------------------- write-time dedupe (Phase 9.2)
        nearest_id: str | None = None
        nearest_sim: float | None = None
        if (
            dedupe_threshold is not None
            and self._vector is not None
            and question_vector is not None
        ):
            try:
                hits = self.recall_similar(
                    query_vector=question_vector,
                    repo_key=repo_key,
                    limit=1,
                    min_similarity=0.0,
                )
            except Exception as e:
                log.warning("dedupe_recall_failed", err=str(e))
                hits = []
            if hits:
                nearest_id = hits[0].memory.id
                nearest_sim = hits[0].similarity
                if hits[0].similarity >= dedupe_threshold:
                    folded = self._fold_into_existing(
                        existing_id=hits[0].memory.id,
                        source=source,
                        origin_agent=resolved_origin_agent,
                        user_id=resolved_user_id,
                    )
                    self._log_memory_write(
                        repo_key=repo_key,
                        user_id=resolved_user_id,
                        origin_agent=resolved_origin_agent,
                        source=source,
                        question_text=question,
                        nearest_existing_id=hits[0].memory.id,
                        nearest_similarity=hits[0].similarity,
                        outcome="folded",
                        resulting_memory_id=hits[0].memory.id,
                        team_id=team_id,
                    )
                    log.info(
                        "memory_folded",
                        existing_id=hits[0].memory.id,
                        similarity=round(hits[0].similarity, 3),
                        source=source,
                    )
                    return folded

        # ----------------------------- INSERT (no dedupe hit, or dedupe off)
        mem_id = str(uuid.uuid4())
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO asil_memories (
                    id, repo_key, question, answer,
                    confidence_score, confidence_evidence_count,
                    confidence_retrieval_strength, confidence_causal_confidence,
                    confidence_derivation, citations,
                    verifier_unsupported, model, provider, cost_usd, profile,
                    metadata,
                    user_id, machine_id, origin_agent, origin_session_id,
                    team_id
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s, %s,
                    %s,
                    %s, %s, %s, %s,
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
                    resolved_user_id,
                    resolved_machine_id,
                    resolved_origin_agent,
                    origin_session_id,
                    team_id,
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
        self._log_memory_write(
            repo_key=repo_key,
            user_id=resolved_user_id,
            origin_agent=resolved_origin_agent,
            source=source,
            question_text=question,
            nearest_existing_id=nearest_id,
            nearest_similarity=nearest_sim,
            outcome="inserted",
            resulting_memory_id=mem_id,
            team_id=team_id,
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
            user_id=resolved_user_id,
            machine_id=resolved_machine_id,
            origin_agent=resolved_origin_agent,
            origin_session_id=origin_session_id,
            team_id=team_id,
        )

    # ------------------------------------------------------------------ helpers

    def _fold_into_existing(
        self,
        *,
        existing_id: str,
        source: str | None,
        origin_agent: str,
        user_id: str,
    ) -> Memory:
        """Bump recall_hits + append the new source to metadata.sources on
        the existing row, then return the rehydrated memory. Used by the
        write-time dedupe path."""
        source_marker = source or f"direct:{origin_agent}:{user_id}"
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE asil_memories
                SET recall_hits = recall_hits + 1,
                    metadata = jsonb_set(
                        metadata,
                        '{sources}',
                        coalesce(metadata->'sources', '[]'::jsonb) || to_jsonb(%s::text),
                        true
                    )
                WHERE id = %s
                """,
                (source_marker, existing_id),
            )
        mem = self.get(existing_id)
        if mem is None:
            raise EpisodicStoreError(
                f"fold target {existing_id!r} vanished mid-UPDATE — db consistency bug"
            )
        return mem

    def _log_memory_write(
        self,
        *,
        repo_key: str,
        user_id: str,
        origin_agent: str,
        source: str | None,
        question_text: str,
        nearest_existing_id: str | None,
        nearest_similarity: float | None,
        outcome: str,
        resulting_memory_id: str,
        team_id: str = "default",
    ) -> None:
        """One row per remember() call. Outcome is 'inserted' or 'folded'.
        Best-effort — never raises into the caller (a logging failure must
        not block the memory write)."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO asil_memory_writes (
                        repo_key, user_id, origin_agent, source, question_text,
                        nearest_existing_id, nearest_similarity, outcome,
                        resulting_memory_id, team_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        repo_key,
                        user_id,
                        origin_agent,
                        source,
                        question_text,
                        nearest_existing_id,
                        nearest_similarity,
                        outcome,
                        resulting_memory_id,
                        team_id,
                    ),
                )
        except Exception as e:
            log.warning("memory_write_log_failed", err=str(e))

    def write_log_stats(self, *, days: int = 30) -> dict[str, Any]:
        """Aggregate dedupe stats from asil_memory_writes. Used by
        `asil memory stats --dedupe-rate` and the /memory dashboard."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT outcome, count(*) FROM asil_memory_writes "
                "WHERE ts >= now() - (%s || ' days')::interval "
                "GROUP BY outcome",
                (days,),
            )
            counts = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute(
                "SELECT origin_agent, count(*) FROM asil_memory_writes "
                "WHERE ts >= now() - (%s || ' days')::interval "
                "GROUP BY origin_agent ORDER BY 2 DESC",
                (days,),
            )
            by_agent = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute(
                "SELECT coalesce(source, '(direct)'), count(*) "
                "FROM asil_memory_writes "
                "WHERE ts >= now() - (%s || ' days')::interval "
                "GROUP BY 1 ORDER BY 2 DESC",
                (days,),
            )
            by_source = {r[0]: int(r[1]) for r in cur.fetchall()}
        inserted = counts.get("inserted", 0)
        folded = counts.get("folded", 0)
        total = inserted + folded
        return {
            "window_days": days,
            "total_writes": total,
            "inserted": inserted,
            "folded": folded,
            "dedupe_rate_pct": round((folded / total) * 100, 2) if total else 0.0,
            "by_agent": by_agent,
            "by_source": by_source,
        }

    def top_recalled(self, *, repo_key: str | None = None, limit: int = 20) -> list[Memory]:
        """Memories with the highest recall_hits. The 'who knows this?' tool."""
        with self._conn.cursor() as cur:
            if repo_key is not None:
                cur.execute(
                    _SELECT_ALL_COLUMNS
                    + " WHERE repo_key = %s ORDER BY recall_hits DESC, created_at DESC LIMIT %s",
                    (repo_key, limit),
                )
            else:
                cur.execute(
                    _SELECT_ALL_COLUMNS
                    + " ORDER BY recall_hits DESC, created_at DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
        return [_row_to_memory(r) for r in rows]

    def bump_recall_hit(self, memory_id: str) -> int:
        """Increment `recall_hits` for one memory. Returns the new count, or 0
        if the row didn't exist. Called by the `asil ask` cache short-circuit
        path so the savings calculator can count real cache hits off the
        ledger instead of estimating."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE asil_memories SET recall_hits = recall_hits + 1 "
                "WHERE id = %s RETURNING recall_hits",
                (memory_id,),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

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
       created_at, metadata, recall_hits,
       user_id, machine_id, origin_agent, origin_session_id,
       team_id
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
        recall_hits,
        user_id,
        machine_id,
        origin_agent,
        origin_session_id,
        team_id,
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
        recall_hits=int(recall_hits or 0),
        user_id=user_id or "unknown",
        machine_id=machine_id or "unknown",
        origin_agent=origin_agent or "cli",
        origin_session_id=origin_session_id,
        team_id=team_id or "default",
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
