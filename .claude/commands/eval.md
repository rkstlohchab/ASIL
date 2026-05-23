---
description: Run the ASIL retrieval recall harness against the indexed code. Use this to check that a change to the embedder, retriever, graph schema, or chunking didn't silently regress retrieval quality.
---

Run the recall eval against the ASIL self-corpus:

```bash
uv run asil eval recall asil_self --repo "local:$(pwd)" --show-details
```

Report back:

1. The recall@1 / recall@3 / recall@5 / recall@10 numbers.
2. Whether they're above or below the Phase 1 baseline (recall@1=60%, recall@5=80%).
3. **Misses, specifically.** For each case with `rank: miss`, show:
   - The question.
   - What the corpus expected (`expected_any`).
   - What the retriever's top hit was instead.
   - One sentence on whether the miss is "retriever bug", "corpus too strict", or "expected gap closed by Phase 2 re-ranker".

Do **not** propose to lower the corpus expectations to make numbers go up. The corpus is a regression catcher; tuning it to hide gaps defeats the purpose. If a real retrieval regression is visible, name the change that likely caused it.

If the eval can't run (Neo4j or Qdrant down), report the failure and stop — don't try to fix the stack from inside this command.
