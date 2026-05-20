---
name: asil-phase-gate
description: Use when proposing or starting new work in ASIL. Checks the proposed work against the current phase and rejects speculative cross-phase code that isn't backed by a passed demo.
---

# asil-phase-gate

Solo founders die by drift. The phase gates are the antidote.

## Hard rule

**Do not start Phase N+1 work until Phase N has:**

1. A recorded demo (video or animated terminal capture).
2. A written design doc in `research/`.
3. Passed its eval bar from the [PLAN.md evaluation table](../../PLAN.md#evaluation--verification).

When tempted to start Phase N+1 early, ask yourself: *"What part of Phase N's demo is missing right now?"* If the answer is anything other than "nothing," go back and finish Phase N.

## The phases

| # | Name | Months | Hero artifact |
|---|---|---|---|
| 0 | Foundation | 0–1 | `make up` + `asil llm ping` |
| 1 | Repo Intelligence | 1–2 | `asil ask` on a real OSS repo |
| 2 | Memory + Confidence | 3 | day-1 / day-7 recall + Confidence on every answer |
| 3 | Infra Bridge | 4–5 | event-ingested postmortem in graph |
| 4 | **Temporal Causality Engine** (moat) | 5–7 | causal recall@5 on postmortem corpus |
| 5 | **Execution Replay + hero demo** | 7–9 | the v1 incident-root-cause demo |
| 6 | Architecture Drift Detection | 9–10 | drift report on a planted violation |
| 7 (stretch) | Minimal UI + MCP polish | 10–12 | Next.js dashboard + hosted demo |
| 8 (stretch) | Deterministic fix pipeline | post-launch | sandbox-validated PR |

## Things often misfiled as Phase 0

These are NOT Phase 0 — pushing them in is drift:

- Tree-sitter parsers, SCIP integration → Phase 1.
- Mem0 / episodic memory wiring → Phase 2.
- K8s adapter, Prometheus scrapers → Phase 3.
- Any causal-edge writing → Phase 4.
- Any UI work → Phase 7.
- Any agent that files PRs → Phase 8.

Phase 0 is **only**: docker compose + LLM router + Confidence + FastAPI/CLI skeletons + CI.

## Things that pull you sideways

Watch for these and push back:

- **"While I'm here, let me also add X"** — write it to `research/design-decisions.md` instead.
- **"This will be useful in Phase 4"** — fine, document it as a deferred TODO. Do not write the code now.
- **"It would be a quick win to ship a UI"** — no. Phase 7. Don't even prototype.
- **"Let me add a second LLM agent here"** — almost certainly wrong. Use a deterministic pipeline step instead. See [asil-llm-call](asil-llm-call.md).

## When the user asks for cross-phase work

If the user requests something that crosses phase boundaries, do this:

1. Acknowledge the request.
2. Identify which phase(s) it belongs to.
3. Check whether the prerequisite phases have passed their gates.
4. If not, surface the gap and suggest finishing the gate first.
5. If they insist anyway, log the decision (commit message, design doc) — but flag the deviation.

The `/phase` slash command shows the current phase and outstanding items.
