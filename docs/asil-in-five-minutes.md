# ASIL in Five Minutes — How "Engineering Intelligence" Works

> **Audience**: engineers, managers, and investors who want to understand
> what ASIL does without reading source code.

---

## The One-Sentence Version

ASIL turns your codebase and incident history into a **knowledge graph**,
then uses that graph to tell you *why things broke* — with confidence
scores, not guesses.

---

## The Problem ASIL Solves

When a production incident happens, engineers face a familiar struggle:

1. **The deploy was 7 minutes ago. The latency spike was 1 minute ago.
   Which one caused the incident?**
   - Traditional tools say "the latency spike" because it's closest in time.
   - ASIL says "the deploy" because it shipped to the same service the
     incident affected — it's the *cause*, not the *symptom*.

2. **Which service broke first? How did the failure cascade?**
   - ASIL reconstructs the full timeline: auth → payments → cart, with
     timestamps and role markers (↗ cause, ▶ INCIDENT, ↓ response).

3. **Can I trust this answer?**
   - Every causal claim ships with a **confidence score** (0–1), a
     **derivation string** (the exact reasoning), and the **strategy**
     that produced it. No black boxes.

---

## How It Works (Three Layers)

### Layer 1: The Code Graph

ASIL parses your source code (Python, JavaScript, TypeScript) with
[Tree-sitter](https://tree-sitter.github.io/tree-sitter/) and builds a
**structural graph** in Neo4j:

```
(:Repo)─[:CONTAINS]→(:File)─[:CONTAINS]→(:Function)
                                         (:Class)
                                         (:Symbol)
(:Function)─[:CALLS]→(:Function)
```

This graph knows which function calls which, which class inherits from
where, and which file defines what. It's the "who" and "where" of your
codebase.

### Layer 2: The Runtime Graph

ASIL ingests **runtime events** — deployments, metric shifts, log
signatures, and incidents — from postmortems (today) or live
Kubernetes/Prometheus/Loki feeds (coming in Phase 6):

```
(:Service)←[:DEPLOYED]─(:Deployment)
(:Service)←[:OBSERVED_IN]─(:MetricShift)
(:Service)←[:EMITTED_BY]─(:LogSignature)
(:Service)←[:AFFECTED]─(:Incident)
```

This graph knows *what happened* in production: which service got a new
deploy, which metric spiked, which logs appeared, and which incidents
were declared.

### Layer 3: The Causality Engine (THE MOAT)

This is what makes ASIL different. The **temporal linker** walks every
event that occurred before an incident and scores each one as a potential
cause:

1. **Temporal proximity**: closer events get higher scores. The decay
   follows an exponential half-life (5 minutes default):
   ```
   confidence(Δt) = exp(−ln2 · Δt / half_life)
   ```

2. **Lagged correlation**: deployments on *affected services* get a
   **+0.6 bonus**. This is the key insight — a deploy on the same
   service the incident hit is a candidate *cause*, while a metric shift
   on the same service is a *symptom*. The bonus is additive, not
   multiplicative, because multiplicative would cap at the original
   proximity score (too low for distant deploys).

The result is a `:PRECEDED` edge in the graph:

```
(:Deployment)─[:PRECEDED {confidence: 0.979,
                           delta_seconds: 420,
                           strategy: "temporal_proximity+lagged_correlation",
                           derivation: "..."}]→(:Incident)
```

Every edge is **observable** — derived from graph state, never from an
LLM prediction. Re-running the linker on the same graph produces the
same edges.

---

## The Hero Demo

```bash
$ asil replay INC-2026-04-12-payments-cascade
```

This command produces:

| Section          | What You See                                            |
|------------------|---------------------------------------------------------|
| **Header**       | Incident title, env, severity, affected services        |
| **Timeline**     | Every event across all affected services, chronological |
| **Top Causes**   | Ranked by confidence, with strategy + derivation        |
| **Cascade**      | Service-to-service propagation order                    |
| **Confidence**   | Aggregate score + evidence count                        |

On our bundled postmortem:

- **Auth deploy** (7min before) → confidence **0.979** (cause)
- **Payments latency spike** (1min before) → confidence **0.871** (symptom)
- **Cascade**: auth → payments → cart

The deploy correctly outranks the spike. That's the cause-vs-symptom
honesty that ASIL's lagged-correlation strategy provides.

---

## What ASIL is NOT

- **Not an "AI that codes."** ASIL doesn't write code. It helps you
  understand code and incidents.
- **Not a chatbot.** The LLM is a reader of the graph, not an author of
  causal claims. Causes come from math, not from GPT's training data.
- **Not a monitoring tool.** ASIL doesn't collect metrics or logs. It
  consumes what your existing tools (Prometheus, Loki, PagerDuty) already
  produce, then connects the dots.

---

## Architecture at a Glance

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│  Tree-sitter │  │  Postmortem   │  │  K8s/Prom    │
│  Parser      │  │  YAML Loader  │  │  Adapter     │
│  (Phase 1)   │  │  (Phase 3)    │  │  (Phase 6)   │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       v                 v                 v
┌─────────────────────────────────────────────────┐
│              Neo4j Knowledge Graph              │
│  Code namespace      │  Runtime namespace       │
│  (Repo/File/Function) │ (Service/Deploy/Metric) │
└──────────┬────────────┴──────────┬──────────────┘
           │                       │
           v                       v
┌──────────────────┐  ┌───────────────────────────┐
│ Hybrid Retriever │  │  Temporal Causality Engine │
│ (vector + graph) │  │  (proximity + lagged corr) │
│ (Phase 2)        │  │  (Phase 4 — THE MOAT)      │
└────────┬─────────┘  └──────────┬────────────────┘
         │                       │
         v                       v
┌─────────────────────────────────────────────────┐
│            Execution Replay Engine              │
│  Timeline | Causes | Cascade | Confidence       │
│  (Phase 5)                                      │
└─────────────────────────────────────────────────┘
```

---

## Current Status

| Phase | Name                    | Status      |
|-------|-------------------------|-------------|
| 0     | Foundation              | ✅ Done     |
| 1     | Repo Intelligence       | ✅ Done     |
| 2     | Reasoning Pipeline      | ✅ Done     |
| 3     | Runtime Events          | ✅ Done     |
| 4     | Temporal Causality      | ✅ Done     |
| 5     | Execution Replay        | ✅ Done     |
| 6     | Live Adapters           | 🔜 Next    |
| 7     | Frontend                | 📋 Planned |

---

## Key Design Decisions

1. **Deterministic over stochastic.** The causality engine uses math
   (exponential decay, additive bonus), not LLM reasoning. This means
   re-running the same analysis produces the same result.

2. **Observable over predicted.** Every causal claim is derived from
   facts already in the graph. The LLM *consumes* causal edges (to
   explain them in natural language); it doesn't *author* them.

3. **Confidence everywhere.** Every conclusion — from code retrieval to
   causal linking — ships with a `Confidence` object: score, evidence
   count, derivation string. No orphan claims.

4. **Graph-first architecture.** Neo4j is the single source of truth for
   structure (code), state (runtime), and causality (`:PRECEDED` edges).
   Vector search (Qdrant) supplements for semantic similarity; episodic
   memory (Postgres) records past conclusions. But the graph is where
   the intelligence lives.

---

*ASIL: Engineering Intelligence Infrastructure. Not an AI OS. Not an
autonomous coder. A system that helps engineers understand why things
happen — with math, not magic.*
