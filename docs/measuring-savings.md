# Measuring savings on your codebase — the A/B protocol

## Why this doc exists

If you're going to claim that ASIL's memory layer saves money, the number has to come from your own ledger, not from an estimate. This guide is the protocol I use to turn "depends" into a real number with provenance.

The mechanism being measured is simple: when a question's embedding is similar enough to a stored prior question (above the cache threshold), ASIL returns the stored answer immediately and skips the reasoning + verifier LLM calls. The only cost on a hit is the one embedding call used to do the lookup. The question this protocol answers is *how often that condition fires on your repo and how much money it saves in practice*.

## What you need

- A working `make up` stack — Neo4j, Qdrant, Postgres reachable.
- An LLM provider configured in `.env` (real keys, not the mock). Mock providers report `cost_usd = 0` and will make the experiment meaningless.
- An ingested codebase. `uv run asil ingest /path/to/repo --embed`.
- ~10 minutes and ~$0.50 of LLM budget for a 20-question run on the `tight` profile. Bigger profiles or bigger corpora scale linearly.
- The seed corpus at `research/savings-benchmark.yaml` (or your own — see "Choosing questions" below).

## The protocol

### Step 0 — pick your conditions

These all materially affect the answer; pin them so the run is reproducible.

| Knob | Where it's set | Recommended for first run |
|---|---|---|
| LLM profile | `ASIL_LLM_PROFILE` in `.env` | `tight` (cheap, deterministic) |
| Cache similarity threshold | `--cache-threshold` on `asil ask` | `0.92` (conservative — fewer hits, but hits are confident) |
| Verifier on/off | `--verify / --no-verify` | `--verify` (matches production behaviour) |
| Question corpus | YAML in `research/` | `research/savings-benchmark.yaml`, 20 questions |
| Profile budget | `ASIL_DAILY_BUDGET_USD` | leave default; the run is well under |

Write these down in a notes file before you start. They go alongside the final number.

### Step 1 — take a marker

```bash
psql "postgresql://asil:asil_dev_password@localhost:5432/asil" -c \
  "SELECT now() AS cold_start_marker;"
```

Save the timestamp it prints. We'll use it to scope ledger sums.

### Step 2 — cold pass (no recall, no remember)

Every question runs the full pipeline. Nothing comes from memory, nothing goes into memory.

```bash
while IFS= read -r q; do
  uv run asil ask "$q" --repo "local:$(pwd)" --no-recall --no-remember > /dev/null
done < <(yq '.questions[].question' research/savings-benchmark.yaml)
```

(If you don't have `yq`, swap in `python -c "import yaml; print('\n'.join(c['question'] for c in yaml.safe_load(open('research/savings-benchmark.yaml'))['questions']))"`.)

### Step 3 — capture the cold totals

```bash
psql "postgresql://asil:asil_dev_password@localhost:5432/asil" <<'SQL'
SELECT
    sum(cost_usd)                     AS cold_total_usd,
    sum(input_tokens + output_tokens) AS cold_total_tokens,
    count(*)                          AS cold_calls
FROM asil_costs
WHERE ts >= TIMESTAMP 'PASTE_COLD_MARKER_HERE';
SQL
```

Write down `cold_total_usd`, `cold_total_tokens`, `cold_calls`.

### Step 4 — warm-population pass

Now we put answers into memory. Same questions, with `--remember` so each conclusion is stored. Leave `--no-recall` for this pass so we're populating, not measuring.

```bash
while IFS= read -r q; do
  uv run asil ask "$q" --repo "local:$(pwd)" --no-recall --remember > /dev/null
done < <(yq '.questions[].question' research/savings-benchmark.yaml)
```

Confirm the memories landed:

```bash
psql "postgresql://asil:asil_dev_password@localhost:5432/asil" -c \
  "SELECT count(*) FROM asil_memories WHERE repo_key = 'local:$(pwd)';"
```

You should see one row per question (or more if there were already prior memories — that's fine).

### Step 5 — take a second marker

```bash
psql "postgresql://asil:asil_dev_password@localhost:5432/asil" -c \
  "SELECT now() AS warm_start_marker;"
```

Save this timestamp too.

### Step 6 — warm pass (recall enabled)

Now run the exact same questions with full recall on. Each one should hit memory.

```bash
while IFS= read -r q; do
  uv run asil ask "$q" --repo "local:$(pwd)" --cache-threshold 0.92 > /dev/null
done < <(yq '.questions[].question' research/savings-benchmark.yaml)
```

### Step 7 — capture the warm totals

```bash
psql "postgresql://asil:asil_dev_password@localhost:5432/asil" <<'SQL'
SELECT
    sum(cost_usd)                     AS warm_total_usd,
    sum(input_tokens + output_tokens) AS warm_total_tokens,
    count(*)                          AS warm_calls
FROM asil_costs
WHERE ts >= TIMESTAMP 'PASTE_WARM_MARKER_HERE';
SQL
```

Write down `warm_total_usd`, `warm_total_tokens`, `warm_calls`.

### Step 8 — compute the real saving

```
usd_saving_pct   = (cold_total_usd   − warm_total_usd)   / cold_total_usd   × 100
token_saving_pct = (cold_total_tokens − warm_total_tokens) / cold_total_tokens × 100
hit_rate_pct     = (cold_calls − warm_calls) / cold_calls × 100
```

On a cache-hit the only call recorded is the embed, so `warm_calls` should be roughly equal to the question count (one embed per question) and `cold_calls` should be ~3-4× higher (retrieve embed + reasoning + verifier + memory-write embed).

These three numbers are what go in your blog post / README / pitch deck. Put them next to the conditions from Step 0 so anyone reading can reproduce.

## Choosing questions

The corpus matters more than people expect. Some guidance:

- **20 questions minimum.** Below that, sampling noise dominates and the percentage swings 10+ points between runs.
- **Mirror what you'd actually ask.** "Where is auth handled?", "What are the callers of X?", "How does the cost ledger work?" — concrete, repo-grounded questions. Not "explain this codebase" type prompts.
- **Avoid trivially-cacheable phrasings.** If every question is identically worded, you're measuring the recall path's high end, not realistic use. Mix in paraphrases.
- **Pin the corpus.** Edits between runs invalidate the comparison.

`research/savings-benchmark.yaml` is a starter — 20 questions hand-curated for this repo. Use it as a template for your own codebase. Same format as `packages/asil_eval/asil_eval/corpus/asil_self.yaml`.

## Things that will skew the number

- **Mock providers** — if `MockLLMProvider` is wired in, every cost is $0 and the saving is undefined. Confirm with `uv run asil llm profile`.
- **Profile mismatch between runs** — if you change `ASIL_LLM_PROFILE` between cold and warm, the numbers aren't comparable.
- **Threshold mismatch** — running cold with `--cache-threshold` set high enough that the warm pass mostly misses defeats the experiment. The threshold only matters in the warm pass.
- **Prior memories** — if `asil_memories` already had hits for these questions before the cold pass, the cold pass might short-circuit too. Either start from a clean memory store (`DELETE FROM asil_memories WHERE repo_key = 'local:...';`) or accept the cold pass is a real-world "no recall flag set" measurement rather than a fresh one.
- **Verifier flakiness** — different verifier outputs across runs can change downstream token counts by a small amount. Token totals are usually stable to within a few percent; spend totals more so.

## Recording the result

After a run, write a one-page summary to `research/savings-benchmark-YYYY-MM-DD.md` with:

- The exact conditions from Step 0.
- Cold and warm totals (USD, tokens, calls).
- The three computed percentages.
- One line of qualitative observation — "21/20 hit because one paraphrase matched a prior unrelated memory" or similar.

That file is what you cite in the Medium post.
