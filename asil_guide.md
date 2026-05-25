# ASIL — What It Does, How to Use It, How It Saves Money

---

## 1. Is It Complete?

**The engine is complete (Phases 0–6).** What remains is stretch/polish work:

| Status | Phase | What It Does |
|--------|-------|-------------|
| ✅ Done | 0 — Foundation | Docker stack, LLM router, config |
| ✅ Done | 1 — Repo Intelligence | Parse any codebase → knowledge graph |
| ✅ Done | 2 — Memory + Confidence | Remember past answers, verify claims |
| ✅ Done | 3 — Infra Bridge | Ingest production events (deploys, metrics, logs) |
| ✅ Done | 4 — Temporal Causality | **THE MOAT** — figures out *what caused what* |
| ✅ Done | 5 — Execution Replay | "Time-travel debugging" — replay any incident |
| ✅ Done | 6 — Architecture Drift | Detect when code structure drifts from baseline |
| ⬜ Stretch | 7 — UI | Web dashboard (not needed — CLI works) |
| ⬜ Stretch | 8 — Auto-fix PRs | Generate fix PRs from causal analysis |

---

## 2. What Can It Do? (Full Capability List)

### 🔍 Code Intelligence
Ask questions about any codebase. ASIL parses the code into a knowledge graph (Neo4j) and vector embeddings (Qdrant), then answers with **cited evidence** and a **confidence score**.

```bash
asil ask "How does the LLM router pick a provider for a given tier?"
# → Returns exact file:line citations, confidence score, verification status
```

### 🧠 Persistent Memory (THE MONEY SAVER)
Every answer ASIL produces is **stored permanently** with full provenance. When you (or any AI agent) asks a similar question later — even in a brand new session — ASIL **recalls the prior answer** instead of re-running the full LLM pipeline.

```bash
asil memory stats          # see how many conclusions are stored
asil memory recall         # view recent memories
```

### 🔬 Incident Causality (THE MOAT)
Given a production incident, ASIL automatically identifies the **root cause** using 3 composable strategies:
- **Temporal proximity** — what deployed right before the incident?
- **Lagged correlation** — which service's metrics correlate with the failure?
- **Explicit reference** — does the postmortem name a specific deployment?

```bash
asil postmortem ingest research/postmortems/2025-08-14-payments-redis-cascade.yaml
asil temporal link prod
asil temporal causes INC-2026-04-12-payments-cascade
```

### 🎬 Incident Replay
Full "time-travel" replay of any incident — timeline, causes, cascade, architecture diff:

```bash
asil replay INC-2026-04-12-payments-cascade
# → 6-panel Rich terminal view: header → timeline → causes → cascade → state diff → confidence
```

### 🏗️ Architecture Drift Detection
Snapshot your codebase's dependency structure, then detect when things drift:

```bash
asil drift baseline myorg/myrepo --output baseline.json
# ... time passes, code changes ...
asil drift report myorg/myrepo --baseline baseline.json
# → Shows new/removed dependencies + boundary violations
```

### 🔌 MCP Tool Surface (12 tools)
Any AI coding agent (Claude, Cursor, Copilot) can call ASIL programmatically:

```
asil.search_code, asil.get_callers, asil.get_dependencies,
asil.who_owns, asil.ask, asil.remember, asil.recall, asil.forget,
asil.find_causes, asil.replay_incident, asil.drift_check, asil.commit_history
```

---

## 3. How to Test It on Your Own Codebase

### Prerequisites
Make sure Docker is running and services are up:
```bash
cd /Users/raksithlochabb/Documents/GitHub/ASIL
make up          # starts Neo4j, Qdrant, Postgres, Redis, Loki, Prometheus, Grafana
uv run asil status   # confirm all services show "ok"
```

### Step 1: Ingest your codebase
Point ASIL at any local repo or GitHub URL:

```bash
# Local repo (e.g., your job-finder project)
uv run asil ingest /path/to/your/project

# Or a GitHub repo
uv run asil ingest https://github.com/your-username/your-repo

# With embeddings for semantic search (costs ~$0.001 per 100 functions)
uv run asil ingest /path/to/your/project --embed
```

This will:
1. Parse all Python/JS/TS/TSX files with Tree-sitter
2. Build a knowledge graph (functions, classes, files, imports, call edges)
3. Optionally embed function bodies for semantic search

### Step 2: Ask questions
```bash
uv run asil ask "What does the main entry point do?"
uv run asil ask "Which functions call the database?"
uv run asil ask "How is authentication handled?"
```

Each answer includes:
- **Citations** — exact file:line references
- **Confidence score** — how reliable the answer is
- **Verification** — a second LLM pass checks every claim

### Step 3: Explore the graph
```bash
uv run asil graph stats              # how many nodes/edges
uv run asil graph neighbors MyClass  # see what connects to a node
uv run asil graph query "MATCH (f:Function) RETURN f.qualified_name LIMIT 10"
```

### Step 4: Check architecture drift
```bash
# Take a baseline snapshot
uv run asil drift baseline your-org/your-repo --output baseline.json

# Later, after making changes, check for drift
uv run asil drift report your-org/your-repo --baseline baseline.json
```

### Step 5: Try incident analysis (with the bundled demos)
```bash
# Ingest a postmortem
uv run asil postmortem ingest research/postmortems/2025-08-14-payments-redis-cascade.yaml

# Run causal analysis
uv run asil temporal link prod

# See the full replay
uv run asil replay INC-2026-04-12-payments-cascade
```

---

## 4. How It Saves API Calls Across Sessions

This is the **Episodic Memory** system (Phase 2). Here's exactly how it works:

### The Problem
Every time you start a new AI chat session (Claude, Cursor, etc.), the AI has **zero memory** of what it figured out before. If you ask "how does auth work?" today and again next week, the AI re-reads the entire codebase, re-calls the LLM, re-verifies — burning the same API credits again.

### ASIL's Solution: Persistent Conclusion Store

```
┌─────────────────────────────────────────────────────────┐
│                    asil ask "question"                   │
│                                                         │
│  1. Embed the question                                  │
│  2. Search episodic memory: "have I answered this?"     │
│     ├─ YES (similarity > threshold)                     │
│     │   → Return cached answer + original confidence    │
│     │   → Cost: ~$0.0001 (embedding only)               │
│     │                                                   │
│     └─ NO (new question)                                │
│         → Full pipeline: vector search → graph expand   │
│           → LLM answer → verify → score → STORE        │
│         → Cost: ~$0.005–0.02 (full LLM round)          │
└─────────────────────────────────────────────────────────┘
```

### Where memories live

| Store | What | Why |
|-------|------|-----|
| **Postgres** (`asil_memories` table) | Full answer, confidence, citations, model, cost, timestamp | Source of truth — survives restarts |
| **Qdrant** (`asil_memories` collection) | Question embedding vector | Semantic similarity search |

### What gets stored per memory

Every conclusion is a row with:
- The original **question**
- The full **answer** with citations
- **Confidence** score (how reliable)
- Which **model** answered (gpt-4o-mini, claude, etc.)
- How much it **cost** in USD
- Full **citation chain** (which files/functions were used as evidence)
- How many claims the **verifier** flagged as unsupported

### How recall works

When a new question comes in:
1. ASIL embeds the question text → vector
2. Searches Qdrant's `asil_memories` collection for similar past questions
3. If similarity > threshold: returns the stored answer (cost: ~1 embedding call)
4. If no match: runs the full pipeline and **stores the result** for next time

### The savings math

| Scenario | Without ASIL Memory | With ASIL Memory |
|----------|---------------------|-------------------|
| Ask "how does auth work?" × 5 sessions | 5 × $0.01 = **$0.05** | 1 × $0.01 + 4 × $0.0001 = **$0.0104** |
| 100 questions asked twice each | 200 × $0.01 = **$2.00** | 100 × $0.01 + 100 × $0.0001 = **$1.01** |
| Team of 3, same codebase, overlapping questions | 3× everything | Shared memory store — **1× cost** |

### MCP integration (for AI agents)

Any AI agent can use the memory tools programmatically:

```json
// Store a conclusion
{"tool": "asil.remember", "args": {"repo_key": "myorg/repo", "question": "...", "answer": "..."}}

// Recall past conclusions
{"tool": "asil.recall", "args": {"query": "how does auth work?", "repo_key": "myorg/repo"}}

// Forget something wrong
{"tool": "asil.forget", "args": {"memory_id": "uuid-here"}}
```

### CLI commands

```bash
# See all stored memories
uv run asil memory stats

# View recent conclusions
uv run asil memory recall --limit 10

# View memories for a specific repo
uv run asil memory recall --repo myorg/myrepo
```

---

## 5. Quick Start Cheat Sheet

```bash
# 1. Start services
make up

# 2. Check health
uv run asil status

# 3. Ingest your code
uv run asil ingest /path/to/your/project --embed

# 4. Ask questions (answers are auto-remembered)
uv run asil ask "What is the main architecture pattern?"

# 5. Ask again later — it's instant (recalled from memory)
uv run asil ask "What is the main architecture pattern?"

# 6. Try the incident demo
uv run asil postmortem ingest research/postmortems/2025-08-14-payments-redis-cascade.yaml
uv run asil temporal link prod
uv run asil replay INC-2026-04-12-payments-cascade

# 7. Snapshot architecture
uv run asil drift baseline myorg/myrepo --output baseline.json
```

> [!TIP]
> The `tight` LLM profile (default) uses the cheapest models. Set `ASIL_LLM_PROFILE=generous` in `.env` for better answers at higher cost.
