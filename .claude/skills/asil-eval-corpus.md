---
name: asil-eval-corpus
description: Use when adding eval cases to a Q&A corpus or building a new corpus. Enforces phrasing-as-developer-questions, `expected_any` semantics, and the anti-pattern of tuning the corpus to mask retrieval gaps.
---

# asil-eval-corpus

The eval corpus is the *regression catcher* for retrieval. It exists so that
when someone "improves" the embedder, the retriever, or the graph schema,
we can detect immediately if it makes things worse. Treat it like a contract,
not a benchmark to game.

## Where corpora live

- `packages/asil_eval/asil_eval/corpus/*.yaml`
- Built-in corpora are registered in `_BUILTIN_CORPORA` in `recall.py`.
- The first shipping corpus is `asil_self.yaml` — ASIL indexing its own code,
  10 hand-curated questions covering each major capability.

## Authoring rules

1. **Phrase like a developer would actually ask, not like a search query.**
   - ❌ `"verify_connectivity Neo4j"`
   - ✅ `"Where do we connect to Neo4j and validate the connection works?"`
   The retriever's job is to bridge that gap. If you phrase too searchy, you stop testing retrieval and start testing keyword matching.

2. **Phrasing diversity is the point.**
   - Mix imperative, descriptive, and behavioral phrasings.
   - Use vocabulary from the user's perspective, not the implementation's. ("how do we avoid duplicates" not "what merges nodes idempotently").

3. **`expected_any` semantics — any one of these is a hit.**
   - List 1–3 qualified names that would all be acceptable answers.
   - Use **short** qnames (the last few segments). The harness matches by suffix.
   - Don't list 10 names hoping at least one will land in top-K. That's gaming.

4. **Add a `notes` field explaining what the case is testing.**
   - Future-you will thank you. Especially when a case starts failing and you need to decide whether the retrieval regressed or the codebase moved.

## Anti-patterns — do not do this

1. **Don't tune the corpus to make recall@K go up.**
   If a question fails, the question is correct — the retriever needs to improve. The Phase 1 baseline ships at 60% recall@3 *honestly*; tuning it to 80% by relaxing `expected_any` would hide the gap that Phase 2's re-ranker is meant to close.

2. **Don't add cases that are trivially answered.**
   `"Where is the GraphStore class defined?"` is not a useful test — the qname `GraphStore` is in the embedding text verbatim. Test semantic understanding, not string matching.

3. **Don't add cases where the answer is "the whole file."**
   Retrieval works at function/class granularity. If the answer is "look at all of graph_store.py," your question is too broad.

4. **Don't ship cases that depend on a specific commit.**
   Q&A should survive minor code churn. If `expected_any` is `["pkg.Class._private_method_v3"]`, it'll break on any refactor.

## What good cases look like

```yaml
- question: "Where is the daily budget enforced before an LLM call goes out?"
  expected_any:
    - "router.ModelRouter._check_budget"
  notes: "Tests precise method-level recall — only one symbol satisfies this."

- question: "How does ASIL avoid creating duplicate nodes when re-ingesting?"
  expected_any:
    - "graph_store.GraphStore.merge_file_with_children"
    - "vector_store.point_id_for"
  notes: "Idempotency lives at two layers — graph MERGE + deterministic point IDs. Either is a valid answer."
```

## Adding a new corpus

1. Create `packages/asil_eval/asil_eval/corpus/<name>.yaml` with the shape from `asil_self.yaml`.
2. Add the file to `_BUILTIN_CORPORA` in `recall.py`.
3. Update `[tool.hatch.build.targets.wheel.force-include]` in the package's `pyproject.toml` so the YAML ships in the wheel.
4. Add a unit test in `tests/unit/test_eval_recall.py` confirming the corpus loads.
5. Run `asil eval recall <new-corpus> --repo <key>` and document the baseline in `research/benchmarks.md`.

## Running the eval

```bash
uv run asil eval recall asil_self --repo "local:$(pwd)" --show-details
```

The `--show-details` flag prints per-case rank, including misses. Look at the misses *first* when debugging a regression — the summary numbers tell you *that* something moved; the misses tell you *what*.
