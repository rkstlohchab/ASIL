# Research notes

Living reading list. Each entry: title, year, one-line takeaway, **how ASIL uses it** (concrete file path or phase).

## Read for Phase 1 (repo intelligence + structural graph)

- **Codebase-Memory: Tree-Sitter Knowledge Graphs** (2026, ArXiv) — Tree-sitter → SQLite graph → MCP tools yields ~10× lower token cost vs file-based exploration. ASIL applies the same Tree-sitter → graph pipeline but writes into Neo4j (richer relational queries) + Qdrant (semantic) instead of SQLite. See [packages/asil_ingest/asil_ingest/treesitter_parser.py](../packages/asil_ingest/asil_ingest/treesitter_parser.py).
- **SCIP — Sourcegraph Code Intelligence Protocol** (2023+) — cross-language symbol resolution as a protobuf wire format. ASIL ingests `scip-python` / `scip-typescript` outputs to enrich the graph beyond what Tree-sitter alone resolves. Planned: `packages/asil_ingest/scip_indexer.py`.
- **GraphCodeBERT** (2020) — historical context for code-aware embeddings; we use Voyage-3-code / BGE-large via the router, not GCB directly, but the paper motivates AST-aware chunking.
- **RepoCoder** (2023) — iterative retrieval at the repo level; informs the hybrid retriever loop in [packages/asil_memory/hybrid_retriever.py](../packages/asil_memory/hybrid_retriever.py) (forthcoming Phase 1.4).

## Read for Phase 2 (memory + confidence)

- **MemGPT / Letta** — virtual context paging; informs the episodic store interface.
- **Generative Agents (Park et al., 2023)** — reflection + episodic memory architecture.
- **GraphRAG (Microsoft, 2024)** — read with a critical lens; hybrid vector+graph beats it in production (2026 evidence).

## Read for Phase 4 (temporal causality engine — the moat)

- **Pearl, *Causality* (intro chapters)** — DAGs, do-calculus, confounders.
- **Hernán & Robins, *What If*** — applied counterfactual reasoning.
- **Change-point detection: PELT (Killick 2012), BOCPD (Adams & MacKay 2007)** — used to emit `MetricShift` nodes from raw Prometheus series.
- **MicroRCA / CausalRCA / GrayHat** — AIOps incident root-cause analysis methods; baseline for ASIL's causal linker.

## Read for Phase 5 (execution replay)

- **KubeIntellect** — supervisor + domain-aligned K8s agents; reference architecture for the infra layer.
- **Public postmortems corpus** — `danluu/post-mortems`, k8s.io issues, GitLab/Cloudflare/AWS public RCAs. Pick 3–5 to ingest as Phase 4 eval data.

## Read before Phase 8 stretch (autonomous fix pipeline)

- **SWE-agent + SWE-bench Verified** — agent execution + critique loops.
- **Sandbox isolation tradeoffs** — gVisor vs Firecracker vs Docker-in-Docker.

---

When you finish reading a paper, append a 3-bullet summary here (claim / method / what we steal) under a `### {short-title}` heading.
