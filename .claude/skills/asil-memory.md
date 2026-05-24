---
name: asil-memory
description: Use when adding or modifying episodic memory (the Postgres + Qdrant store of past `asil ask` conclusions), or when wiring a new feature that should write to or read from memory. Enforces the provenance contract, the dim-stability rule, and the source-of-truth invariant.
---

# asil-memory

Episodic memory is ASIL's "I already concluded that" layer. Every answer
written to it carries enough provenance to be reproduced — question,
answer, confidence, citations, model, cost, verifier breakdown. Future
runs use it to recall past conclusions before re-deriving them.

## Where it lives

- `packages/asil_memory/asil_memory/episodic.py` — `EpisodicStore`, `Memory`, `MemoryHit`.
- Postgres schema: one table `asil_memories` (created by `apply_schema()`, idempotent).
- Qdrant collection: `asil_memories` (constant `EPISODIC_COLLECTION`), one point per memory, keyed by the same UUID as the Postgres row.

## Hard rules

1. **Postgres is the source of truth.** If Qdrant is unreachable, the memory still persists (write succeeds; semantic recall just degrades to recent-only until the next sync). Inverse — Qdrant up, Postgres down — refuses the write.

2. **Every memory carries full provenance.** Don't add a memory write path that drops fields. The Memory dataclass enumerates the contract. If you need to attach extra context, use the `metadata` JSONB column — never overload existing fields.

3. **The Qdrant point ID is the Postgres UUID.** Don't introduce a translation table. Same id everywhere → no join required, no drift possible.

4. **Out-of-band writes (`asil.remember` MCP tool) get a low baseline confidence (0.5, evidence_count=0).** This prevents them from outranking verifier-validated `asil.ask` conclusions when recalled.

5. **Memory persistence is best-effort from a UX standpoint, but transactional from a data standpoint.** Errors should log + degrade gracefully (the user still gets their answer), but never half-write (Postgres without Qdrant catch-up info, or Qdrant point with no Postgres row).

## Dim-stability invariant

The `asil_memories` Qdrant collection's dim is set by whichever embedder
created it first. Changing embedding profiles mid-life means the existing
collection is incompatible. The store raises `VectorStoreError` rather than
silently dropping vectors.

**Implications:**

- Integration tests that touch `EPISODIC_COLLECTION` must clean up after themselves (the `vector_store` session fixture in `tests/integration/conftest.py` deletes the collection on teardown).
- Switching from `text-embedding-3-small` (1536) to `voyage-3-code` (1024) requires `asil memory clear <repo>` for all repos followed by re-asking the questions, or manually deleting the collection. There's no auto-migration.

## How to add a new memory-using feature

1. Construct an `EpisodicStore(vector_store=...)`. Always pass `vector_store=` so semantic recall is enabled — don't construct without it unless you're explicitly building a "metadata only" tool.
2. Call `.apply_schema()` once at startup (idempotent, cheap).
3. For writes: build a `Confidence` object first (use the canonical scorer in `asil_reasoning`), then call `remember(...)`. Pass `question_vector=` if you've already computed it; otherwise the recall side won't find this memory until embedded.
4. For semantic recall: `recall_similar(query_vector=..., repo_key=..., min_similarity=0.85)`. The 0.85 threshold is the default in `asil ask`; below that you typically get false positives.
5. For recent recall (e.g., "what did we conclude in the last 24 hours"): `recall_recent(repo_key=..., limit=...)`.
6. For mutations from agents: route through the MCP tools (`asil.remember`, `asil.forget`). The CLI's `asil memory` group exists for humans; the MCP surface is for agents.

## Forbidden patterns

```python
# ❌ Constructing memories with a stripped Confidence
estore.remember(..., confidence=Confidence(score=0.5))  # no evidence_count, no derivation
# Use a real scorer output or Confidence.unknown().

# ❌ Storing the question vector separately from the memory
mem = estore.remember(..., question_vector=None)
qstore.upsert(...)  # don't do this — `remember()` owns both writes.

# ❌ Recalling without a repo_key when you have one
hits = estore.recall_similar(query_vector=v)  # cross-repo bleed.
# Always pass repo_key when the caller knows it.

# ❌ Using the Mem0/Letta libraries
# We chose Postgres+Qdrant deliberately (see episodic.py docstring); don't
# re-introduce a vendor abstraction without a design doc justifying the swap.
```

## Recall threshold tuning

`asil ask` uses `min_similarity=0.85` for prior-conclusion recall. Lower
values surface false positives (the same word reappearing in unrelated
questions); higher values miss real repeats. Don't tune this without
running the eval harness against the day-1/day-7 demo bar from PLAN.md.

If you find yourself wanting to tune per-feature, the threshold should
become a config knob in `asil_core.config`, not a magic number scattered
across CLI command code.

## When to grow the schema

Adding a column to `asil_memories` is fine but commits you to a migration
story. Phase 2 has no migration framework yet (we just `CREATE TABLE IF NOT
EXISTS`); adding columns requires a manual `ALTER TABLE` step or a Phase 2.x
introduction of Alembic / pgmigrate. If your change adds fields, propose
the migration approach in the same PR.
