# AGENTS.md — orientation for any AI coding agent

Whichever tool you're running in (Claude Code, Antigravity, Cursor, OpenHands, Aider, Continue, Cody), read these files **in this order** before changing anything:

1. **[CLAUDE.md](CLAUDE.md)** — project conventions, hard rules, devloop. Load-bearing.
2. **[PLAN.md](PLAN.md)** — phased roadmap with current status. Tells you which phase you're in.
3. **[.claude/skills/](.claude/skills/)** — workflow rules (also valid for non-Claude tools):
   - [asil-llm-call.md](.claude/skills/asil-llm-call.md) — every LLM call goes through `ModelRouter.call(tier=...)`. No hardcoded model names.
   - [asil-confidence.md](.claude/skills/asil-confidence.md) — every conclusion ships with a `Confidence` object.
   - [asil-positioning.md](.claude/skills/asil-positioning.md) — "Engineering Intelligence Infrastructure," never "AI OS" / "autonomous coder."
   - [asil-phase-gate.md](.claude/skills/asil-phase-gate.md) — don't start Phase N+1 until N has demoed.
   - [asil-graph-schema.md](.claude/skills/asil-graph-schema.md) — Neo4j schema invariants: `repo_key` on every node, MERGE-based idempotency, JSON-string properties for un-promoted relations.
   - [asil-mcp-tool.md](.claude/skills/asil-mcp-tool.md) — MCP tool contract: async, JSON-safe, Confidence on every reasoning result, read-only by default.
   - [asil-eval-corpus.md](.claude/skills/asil-eval-corpus.md) — don't tune the corpus to hide retrieval gaps; the eval is a regression catcher.
   - [asil-memory.md](.claude/skills/asil-memory.md) — episodic-store invariants: Postgres is source of truth, Qdrant point ID == Postgres UUID, full provenance on every memory.
   - [asil-runtime-events.md](.claude/skills/asil-runtime-events.md) — runtime-namespace schema (Service/Deployment/MetricShift/LogSignature/Incident under `env_key`), isolated from the code namespace, no causal edges in Phase 3.
   - [asil-temporal-causality.md](.claude/skills/asil-temporal-causality.md) — THE MOAT. `:PRECEDED` edge contract, observable-only causality (no LLM-emitted causes), confidence + derivation + strategy always logged, cause-vs-symptom honesty.
4. **[docs/phase-0-testing.md](docs/phase-0-testing.md)** and **[docs/phase-1-testing.md](docs/phase-1-testing.md)** — local validation checklists. Phase 1's guide is the freshest pattern.

## The five hard rules

1. All LLM calls go through `asil_core.llm.ModelRouter.call(tier=...)`. No hardcoded model names.
2. Every conclusion ships with a `Confidence` object — never strip it.
3. Deterministic pipelines over multi-agent debate. One critique pass max.
4. No frontend / Next.js work until Phase 7.
5. Phase gates: don't start Phase N+1 until N has a demo + design doc in `research/`.

## Current status

**Phases 0–6 ✅ DONE (2026-05-25).** The engine work is complete: code intelligence, memory, infra bridge, temporal causality (THE MOAT), execution replay, and architecture drift detection. Next: Phase 7 (stretch) — Minimal UI + MCP polish. See [PLAN.md](PLAN.md) for the full roadmap.

## Devloop

```bash
make bootstrap          # uv sync + create .env
make up                 # start docker services (neo4j, qdrant, postgres, redis, loki, prom, grafana)
make test               # unit tests
make lint               # ruff check
make format             # ruff format + ruff check --fix
uv run asil status      # service health
uv run uvicorn asil_api.main:app --reload   # API on :8000
```

## When in doubt

- Plan-level questions → [PLAN.md](PLAN.md)
- Convention / how to write code → [CLAUDE.md](CLAUDE.md)
- Positioning / naming / framing → [.claude/skills/asil-positioning.md](.claude/skills/asil-positioning.md)
- Whether a piece of work is in scope right now → [.claude/skills/asil-phase-gate.md](.claude/skills/asil-phase-gate.md)
- Phase 0 sanity-check on a fresh machine → [docs/phase-0-testing.md](docs/phase-0-testing.md)
