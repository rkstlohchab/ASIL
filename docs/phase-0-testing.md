# Phase 0 — Local validation guide

This is the checklist that proves Phase 0 actually works on your machine. Run each step in order; each one has an expected output and a failure-mode guide.

**Phase 0 demo bar** (from [PLAN.md](../PLAN.md)): `make up`, `asil status` shows all services reachable, `asil llm ping --tier reasoning` returns a response with cost logged.

---

## Prereqs (one-time)

You need:

| Tool | Why | Install |
|---|---|---|
| Docker Desktop | Runs Neo4j / Qdrant / Postgres / Redis / Loki / Prometheus / Grafana | <https://www.docker.com/products/docker-desktop/> |
| `uv` | Python package + workspace manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Python 3.12+ | Runtime | `uv python install 3.12` |
| (optional) DeepSeek API key | Real LLM round-trip on `tight` profile | <https://platform.deepseek.com/> |

Verify:

```bash
docker --version          # 24.x or newer
uv --version              # 0.4.x or newer
```

---

## Step 1 — Bootstrap

```bash
cd /Users/raksithlochabb/Documents/GitHub/ASIL
make bootstrap
```

**What it does:** installs all workspace deps via `uv sync` + copies `.env.example` → `.env` if it doesn't exist yet.

**Expected output:**
```
uv sync
Resolved N packages in …
Installed N packages in …
```
Plus a `.env` file at the repo root.

**Failures:**
- *`uv: command not found`* → install uv first (see prereqs).
- *`Python 3.12+ required`* → `uv python install 3.12`.
- *Network errors during sync* → check your connection / proxy.

After this step, edit `.env`:

```bash
# minimum to get a real LLM round-trip on the tight profile:
DEEPSEEK_API_KEY=sk-...
```

If you skip the API key, `asil llm ping` still works but uses the `MockLLMProvider` and returns `"ok"` with $0 cost.

---

## Step 2 — Start backing services

```bash
make up
```

**What it does:** `docker compose up -d` brings up 7 services in the background.

**Expected output:** a list of containers starting, then endpoint URLs printed:

```
 ✔ Container asil-neo4j       Started
 ✔ Container asil-qdrant      Started
 ✔ Container asil-postgres    Started
 ✔ Container asil-redis       Started
 ✔ Container asil-loki        Started
 ✔ Container asil-prometheus  Started
 ✔ Container asil-grafana     Started

Services starting. Endpoints:
  Neo4j browser   http://localhost:7474   (neo4j / asil_dev_password)
  Qdrant          http://localhost:6333/dashboard
  Postgres        localhost:5432          (asil / asil_dev_password / asil)
  ...
```

**Verify each endpoint manually:**

| Service | URL | What you should see |
|---|---|---|
| Neo4j | <http://localhost:7474> | Neo4j Browser login screen |
| Qdrant | <http://localhost:6333/dashboard> | Qdrant dashboard (collections empty) |
| Prometheus | <http://localhost:9090> | Prometheus UI |
| Grafana | <http://localhost:3000> | Grafana login (admin / asil_dev_password) |
| Loki | <http://localhost:3100/ready> | text "ready" |

**Failures:**
- *Port already in use (e.g. 5432, 6379)* → another Postgres/Redis is running. Stop it or change the host port mapping in `docker-compose.yml`.
- *Neo4j stays "starting" for a long time* → first boot takes ~30s; check `docker logs asil-neo4j`.
- *Docker daemon not running* → start Docker Desktop.

Tail logs to debug:

```bash
make logs        # tails all services
docker logs asil-neo4j      # one service
```

---

## Step 3 — Run unit tests

```bash
make test-unit
```

**What it does:** runs `pytest tests/unit -v`. No external services required (uses mock providers).

**Expected output:** all green — about 9 tests:

```
tests/unit/test_confidence.py::test_basic_construction PASSED
tests/unit/test_confidence.py::test_unknown_factory PASSED
tests/unit/test_confidence.py::test_rejects_out_of_range_score[-0.1] PASSED
tests/unit/test_confidence.py::test_rejects_out_of_range_score[1.5] PASSED
tests/unit/test_confidence.py::test_rejects_negative_evidence_count PASSED
tests/unit/test_llm_router.py::test_router_dispatches_to_provider_for_tier PASSED
tests/unit/test_llm_router.py::test_router_records_cost_in_ledger PASSED
tests/unit/test_llm_router.py::test_router_downgrades_to_fallback_when_budget_exceeded PASSED
tests/unit/test_llm_router.py::test_router_embed_uses_embedding_provider PASSED
tests/unit/test_llm_router.py::test_router_rejects_embed_via_call PASSED

========= 10 passed in X.XXs =========
```

**Failures:**
- *`ModuleNotFoundError: asil_core`* → `make sync` (uv didn't install workspace members in editable mode).
- *Tests hang* → likely an asyncio issue; check `asyncio_mode = "auto"` is set in root `pyproject.toml`.

---

## Step 4 — Lint + format check

```bash
make lint
make format       # auto-formats; use `uv run ruff format --check .` for read-only
```

**Expected:** `All checks passed!` (no rule violations).

If `make lint` fails, the CI workflow will reject your push. Run `make format` to auto-fix where possible.

---

## Step 5 — Service status via CLI

```bash
uv run asil status
```

**Expected output:** a table with every service in green:

```
                 ASIL service status
┌────────────┬────────────────────────────────┬────────┬──────────┐
│ service    │ url                            │ status │ detail   │
├────────────┼────────────────────────────────┼────────┼──────────┤
│ neo4j      │ http://localhost:7474          │ ok     │ HTTP 200 │
│ qdrant     │ http://localhost:6333          │ ok     │ HTTP 200 │
│ prometheus │ http://localhost:9090          │ ok     │ HTTP 302 │
│ loki       │ http://localhost:3100/ready    │ ok     │ HTTP 200 │
│ grafana    │ http://localhost:3000/api/...  │ ok     │ HTTP 200 │
└────────────┴────────────────────────────────┴────────┴──────────┘

active LLM profile: tight
```

**Failures:**
- *One row red* → the corresponding container is still booting (`docker logs asil-<name>`) or its port is blocked.
- *All red* → docker compose isn't running; `make up` first.

---

## Step 6 — Show the active LLM profile

```bash
uv run asil llm profile
```

**Expected output:** a table mapping each tier to its provider and model.

Without DeepSeek key in `.env`:
```
              profile: tight
┌───────────┬──────────┬──────────────────────────┐
│ tier      │ provider │ model                    │
├───────────┼──────────┼──────────────────────────┤
│ reasoning │ mock     │ deepseek-chat (mocked)   │
│ classify  │ mock     │ deepseek-chat (mocked)   │
│ summarize │ mock     │ deepseek-chat (mocked)   │
│ verify    │ mock     │ deepseek-chat (mocked)   │
│ embed     │ local    │ bge-large (dim=1024)     │
└───────────┴──────────┴──────────────────────────┘
```

With DeepSeek key:
```
│ reasoning │ deepseek │ deepseek-chat            │
│ classify  │ deepseek │ deepseek-chat            │
...
```

To test the `balanced` or `generous` profiles, set `ASIL_LLM_PROFILE=balanced` in `.env` and add the relevant API keys (Anthropic, Voyage). The CLI re-reads `.env` on each invocation.

---

## Step 7 — Round-trip an LLM call

```bash
uv run asil llm ping --tier reasoning --prompt "Say hi in five words."
```

**Expected output (with real DeepSeek key):**

```
            llm ping — tier=reasoning
┌────────────────┬────────────────────────┐
│ field          │ value                  │
├────────────────┼────────────────────────┤
│ profile        │ tight                  │
│ provider       │ deepseek               │
│ model          │ deepseek-chat          │
│ input_tokens   │ 14                     │
│ output_tokens  │ 7                      │
│ cost_usd       │ 0.000012               │
└────────────────┴────────────────────────┘

response:
Hello, nice to meet you.
```

**Expected output (no DeepSeek key — mock provider):**

```
│ provider       │ mock                   │
│ model          │ deepseek-chat (mocked) │
│ cost_usd       │ 0.000000               │
...
response:
ok
```

**Failures:**
- *401 / authentication error* → DEEPSEEK_API_KEY in `.env` is wrong or unset.
- *Connection timeout* → outbound network issue.
- *Budget exceeded log line* → you set `ASIL_DAILY_BUDGET_USD` very low and the router downgraded to fallback. Expected behavior.

---

## Step 8 — Start the API server

```bash
uv run uvicorn asil_api.main:app --reload
```

Then in another terminal:

```bash
# overall health
curl http://localhost:8000/health | jq

# active MCP info (Phase 0 = 0 tools)
curl http://localhost:8000/mcp/info | jq

# LLM round-trip via HTTP
curl -X POST http://localhost:8000/llm/ping \
  -H "Content-Type: application/json" \
  -d '{"prompt":"hi","tier":"reasoning"}' | jq
```

Browse the OpenAPI docs at <http://localhost:8000/docs>.

---

## What "Phase 0 demo passed" looks like

You can answer "yes" to **all** of these:

- [ ] `make bootstrap && make up` completes cleanly on a fresh checkout.
- [ ] `uv run pytest tests/unit -v` shows all 10 tests green.
- [ ] `uv run asil status` shows all 5 services green.
- [ ] `uv run asil llm profile` prints the tier → provider mapping.
- [ ] `uv run asil llm ping --tier reasoning` returns a non-empty response and a cost > 0 (with real key) or 0 (with mock).
- [ ] `curl http://localhost:8000/health` returns `status: "ok"`.
- [ ] CI passes on the latest commit (GitHub Actions on push to `main`).

If all green, record a 30-second screen capture for the devlog. That's the Phase 0 milestone.

---

## Tear-down

```bash
make down            # stop services, preserve data
make reset-dbs       # DESTRUCTIVE — also removes volumes
```

`reset-dbs` is what you want before re-seeding the demo repo / incident in Phase 1+.
