# Design decisions log

Append-only log of non-obvious choices. Each entry: date, decision, **why**, what was rejected.

---

## 2026-05-19 — Engineering Intelligence Infrastructure positioning (not "AI OS")

**Decision.** ASIL is "Engineering Intelligence Infrastructure," sitting underneath coding agents (OpenHands, Cursor, Claude Code, Aider) rather than competing with them.

**Why.** The autonomous-coding-agent space is crowded and commodifying (OpenHands has 72k stars + $18.8M Series A in 2026). ASIL's defensible niche is temporal causality + execution replay + confidence-scored reasoning — the layer agents *consume* via MCP, not a competing agent.

**Rejected.** "AI OS," "AI engineer," "autonomous coder" framings — pull us back into the commodity bucket.

## 2026-05-19 — Deterministic pipelines, not multi-agent debate

**Decision.** Reasoning runs as a fixed 8-step pipeline (Retrieve → Graph expand → Temporal correlate → Causal score → Reason → Verify → Score → Respond). LangGraph manages state + checkpoints. No "agents debating each other."

**Why.** Multi-agent debate is hype-heavy and expensive. Deterministic pipelines are debuggable, cost-bounded, and replayable. The 2026 trend among production agent systems is fewer agents + better state machines.

**Rejected.** AutoGen-style chatty agents; "DebugAgent argues with VerifierAgent."

## 2026-05-19 — Tier-routed LLM abstraction in Phase 0

**Decision.** Every LLM call goes through `ModelRouter.call(tier=...)` from day 1. Three profiles (tight / balanced / generous), tier-tagged at every call site, never hardcoded model names.

**Why.** User is on a tight budget now but architecture must scale to generous later. Tier-tagging makes profile a config flip, not a refactor. Building this in Phase 0 avoids paying the migration tax in Phase 4+. See [packages/asil_core/asil_core/llm/router.py](../packages/asil_core/asil_core/llm/router.py).

**Rejected.** Late introduction of an abstraction after we'd already sprinkled `anthropic.messages.create()` across the codebase.

## 2026-05-20 — Confidence object as a cross-cutting concern, not optional

**Decision.** Every conclusion ASIL emits — retrieval result, causal claim, root-cause hypothesis — ships with a `Confidence` object containing score, evidence_count, retrieval_strength, causal_confidence, derivation list.

**Why.** This is the differentiator. The hero demo (root cause with confidence) hangs on it. Optional confidence means optional credibility.

**Rejected.** Adding confidence "later, in Phase 5." The cost of retrofitting is enormous; better to bake it in.

## 2026-05-20 — Non-buildable workspace root

**Decision.** Root `pyproject.toml` has no `[project]` or `[build-system]` — it's a pure uv workspace coordinator. Buildable packages live under `apps/*` and `packages/*`.

**Why.** Hatchling failed to build the root because there's no top-level `asil/` source dir, blocking `uv sync`. Removing the project metadata at the root makes the structure honest: the root is a workspace, not a package.

**Rejected.** Adding a placeholder `[tool.hatch.build.targets.wheel] only-include = []` to suppress the error. Less explicit.

## 2026-05-23 — Tree-sitter via `tree-sitter-language-pack`, with a small node shim

**Decision.** Use `tree-sitter-language-pack` for prebuilt binaries (Python, JS, TS, Go, etc.). Isolate the binding's method-based API (`node.kind()`, `node.start_position()`, `node.named_child(i)`) behind a small shim at the bottom of [treesitter_parser.py](../packages/asil_ingest/asil_ingest/treesitter_parser.py) so per-language extractors stay readable.

**Why.** Avoids per-contributor `tree-sitter` CLI build steps. The shim cost is ~30 lines; the alternative is sprinkling `.kind()` everywhere and reading less naturally. Also: tree-sitter-language-pack ships a Rust-backed binding whose API differs from the older py-tree-sitter; isolating it limits blast radius if we swap bindings later.

**Rejected.** Bare `tree-sitter` + per-language packages (build steps per contributor). Older py-tree-sitter (different API, less maintained in 2026).

## 2026-05-23 — Qualified names computed inside the parser, not by the graph builder

**Decision.** `TreeSitterParser` already populates `qualified_name` on functions, classes, and symbols (e.g., `asil_core.llm.router.ModelRouter.call`).

**Why.** Keeps `ParsedFile` self-contained so any downstream consumer (graph builder, embedder, MCP tool) can look up `Class.method` without re-implementing qualified-name logic. Also makes the parser independently testable.

**Rejected.** Two-pass design where the graph builder computes qualified names. Couples two layers unnecessarily.

## 2026-05-23 — Permissive parsing, errors recorded not raised

**Decision.** Tree-sitter errors are recorded in `ParsedFile.parse_errors`; the parser never raises on syntax errors.

**Why.** Real-world repos contain novel constructs and partial code. Indexing 95% of a file is better than 0%. We can downgrade confidence for results sourced from partially-broken files, but we still want them in the graph.

**Rejected.** Strict parser that refuses files with any error.
