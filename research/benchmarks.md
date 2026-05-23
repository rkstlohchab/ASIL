# Benchmarks

Phase-by-phase eval results. Each entry: date, phase, what was measured, result, pass/fail vs PLAN.md bar.

---

## Phase 0 (Foundation) — 2026-05-20

Demo bar (from [PLAN.md](../PLAN.md#phase-0--foundation-weeks-12--done-2026-05-20)):
- `make up` brings up all services
- `asil status` shows reachable
- `asil llm ping --tier reasoning` returns a response with cost logged

**Result:** ✅ passed locally with `OPENAI_API_KEY` configured.

- 7 docker services healthy.
- 10/10 Phase 0 unit tests green.
- `/health` returns `status: "ok"` for all 4 backends.
- `/llm/ping` round-trips through gpt-4o-mini at ~$7e-06 per tiny prompt.

## Phase 1 — milestone 1.1: Python parser — 2026-05-23

Not a formal eval (no PLAN.md bar at the milestone level), but documenting the test surface:

- 11/11 parser unit tests green covering: empty file, top-level function, async function, class with methods, imports (plain / aliased / `from x import y as z` / relative), call-site extraction inside functions, decorators (on functions and classes), symbol collection with qualified names, parse-error tolerance, LOC counting, unimplemented-language guard.

**Open:** the full Phase 1 eval — code-search top-3 recall ≥ 80% on a 50 Q&A set against a real OSS repo — requires the graph builder + embeddings + retriever (milestones 1.2–1.4). Will land when those do.
