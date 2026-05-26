"""ASIL FastAPI gateway.

Phase 0: /health endpoint that reports reachability of every backing service,
plus a /llm/ping endpoint to verify the active LLM profile works end-to-end.

OpenTelemetry hooks are wired but the exporter is conditional on
OTEL_EXPORTER_OTLP_ENDPOINT being set + reachable.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from asil_core import configure_logging, get_logger, get_settings
from asil_core.llm import ModelRouter
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

log = get_logger(__name__)


class ServiceHealth(BaseModel):
    name: str
    status: Literal["ok", "down", "unknown"]
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    services: list[ServiceHealth]
    active_llm_profile: str


class LLMPingRequest(BaseModel):
    tier: Literal["reasoning", "classify", "summarize", "verify"] = "reasoning"
    prompt: str = "Say hi."


class LLMPingResponse(BaseModel):
    text: str
    provider: str
    model: str
    profile: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    app.state.settings = settings
    app.state.router = ModelRouter.from_env()
    log.info(
        "api_started",
        env=settings.asil_env,
        llm_profile=settings.asil_llm_profile,
    )
    yield
    log.info("api_stopped")


app = FastAPI(title="ASIL API", version="0.0.1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    settings = get_settings()
    results = await asyncio.gather(
        _check_neo4j(settings.neo4j_uri),
        _check_http(settings.qdrant_url, name="qdrant"),
        _check_http(settings.prometheus_url, name="prometheus"),
        _check_http(settings.loki_url + "/ready", name="loki"),
        return_exceptions=True,
    )
    services: list[ServiceHealth] = []
    for r in results:
        if isinstance(r, ServiceHealth):
            services.append(r)
        else:
            services.append(ServiceHealth(name="?", status="unknown", detail=str(r)))

    overall = "ok" if all(s.status == "ok" for s in services) else "degraded"
    return HealthResponse(
        status=overall,
        services=services,
        active_llm_profile=app.state.router.active_profile_name,
    )


async def _check_http(url: str, *, name: str, timeout: float = 2.0) -> ServiceHealth:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
        if r.status_code < 500:
            return ServiceHealth(name=name, status="ok", detail=f"HTTP {r.status_code}")
        return ServiceHealth(name=name, status="down", detail=f"HTTP {r.status_code}")
    except Exception as e:
        return ServiceHealth(name=name, status="down", detail=type(e).__name__)


async def _check_neo4j(uri: str) -> ServiceHealth:
    # Bolt isn't HTTP — Phase 0 just probes the HTTP browser endpoint as a proxy.
    host = uri.split("://", 1)[-1].split(":", 1)[0]
    return await _check_http(f"http://{host}:7474", name="neo4j")


@app.post("/llm/ping", response_model=LLMPingResponse)
async def llm_ping(req: LLMPingRequest) -> LLMPingResponse:
    router: ModelRouter = app.state.router
    resp = await router.call(
        tier=req.tier,
        messages=[{"role": "user", "content": req.prompt}],
        max_tokens=64,
    )
    return LLMPingResponse(
        text=resp.text,
        provider=resp.provider,
        model=resp.model,
        profile=router.active_profile_name,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cost_usd=resp.cost_usd,
    )


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "name": "ASIL",
        "description": "Engineering Intelligence Infrastructure",
        "version": "0.0.1",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/mcp/info")
async def mcp_info() -> dict[str, Any]:
    from asil_api.mcp_server import server_info
    from asil_api.mcp_tools import TOOL_CATALOG

    info = server_info()
    info["tools_available"] = len(TOOL_CATALOG)
    info["transport"] = "http"  # native stdio MCP shipping in Phase 7
    return info


@app.get("/mcp/tools")
async def mcp_tools() -> list[dict[str, Any]]:
    from asil_api.mcp_tools import tool_catalog

    return tool_catalog()


class _ToolCallRequest(BaseModel):
    arguments: dict[str, Any] = {}


@app.get("/dashboard/stats")
async def dashboard_stats() -> dict[str, Any]:
    """Aggregated counts for the Phase 7 dashboard. One query per backing store."""
    from asil_memory import EpisodicStore, GraphStore

    code: dict[str, int] = {}
    runtime: dict[str, int] = {}
    repos: list[dict[str, Any]] = []
    envs: list[str] = []
    memory_count = 0

    try:
        with GraphStore() as g:
            g.verify_connectivity()
            code = g.stats()
            runtime = g.runtime_stats()
            repos = g.list_repos()
            envs = [
                row["env_key"]
                for row in g.query(
                    "MATCH (n) WHERE n.env_key IS NOT NULL "
                    "RETURN DISTINCT n.env_key AS env_key ORDER BY env_key"
                )
            ]
    except Exception as e:
        log.warning("dashboard_graph_unavailable", error=str(e))

    try:
        with EpisodicStore() as e:
            e.verify_connectivity()
            e.apply_schema()
            memory_count = e.count()
    except Exception as exc:
        log.warning("dashboard_episodic_unavailable", error=str(exc))

    return {
        "code": code,
        "runtime": runtime,
        "repos": repos,
        "envs": envs,
        "memory_count": memory_count,
        "llm_profile": app.state.router.active_profile_name,
    }


@app.get("/dashboard/cost")
async def dashboard_cost(days: int = 30) -> dict[str, Any]:
    """LLM cost aggregates + episodic-memory savings, for the /cost UI page.

    Returns zero-valued fields if Postgres isn't reachable so the UI can render
    a useful empty state instead of erroring.
    """
    from asil_core.llm.postgres_ledger import from_settings_or_none
    from asil_memory import EpisodicStore

    out: dict[str, Any] = {
        "days": days,
        "ledger_available": False,
        "total_usd": 0.0,
        "calls": 0,
        "by_provider": {},
        "by_tier": {},
        "by_day": [],
        "memory_count": 0,
        "savings": None,
    }

    ledger = from_settings_or_none()
    if ledger is not None:
        agg = ledger.aggregates(days=days)
        out["ledger_available"] = True
        out["total_usd"] = round(agg.total_usd, 4)
        out["calls"] = agg.calls
        out["by_provider"] = {k: round(v, 4) for k, v in agg.by_provider.items()}
        out["by_tier"] = {k: round(v, 4) for k, v in agg.by_tier.items()}
        out["by_day"] = [{"day": d, "cost": round(c, 4)} for d, c in agg.by_day]

    try:
        with EpisodicStore() as e:
            e.verify_connectivity()
            e.apply_schema()
            out["memory_count"] = e.count()
    except Exception as exc:
        log.warning("dashboard_cost_episodic_unavailable", error=str(exc))

    if ledger is not None:
        out["savings"] = ledger.savings_vs_no_memory(out["memory_count"])

    return out


@app.get("/incidents")
async def list_incidents(env_key: str | None = None) -> dict[str, Any]:
    """Every Incident node, newest first. Used by the UI's /incidents page."""
    from asil_memory import GraphStore

    # The canonical id property on :Incident nodes is `id` (set by the
    # postmortem ingestor and merge_incident). External adapters that
    # auto-create incidents from chat/ticket mentions also set `id`. We
    # `coalesce` so the API stays robust even if older nodes ever land
    # with an `incident_id` property instead — and so the UI never sees
    # two rows sharing a `null` key (which is the React duplicate-key bug).
    where = "WHERE i.env_key = $env" if env_key else ""
    cypher = (
        f"MATCH (i:Incident) {where} "
        "OPTIONAL MATCH (i)-[:AFFECTS]->(s:Service) "
        "WITH i, collect(DISTINCT s.name) AS services "
        "RETURN coalesce(i.incident_id, i.id) AS incident_id, "
        "i.detected_at AS detected_at, "
        "i.severity AS severity, i.summary AS summary, i.env_key AS env_key, "
        "services "
        "ORDER BY i.detected_at DESC"
    )
    try:
        with GraphStore() as g:
            g.verify_connectivity()
            params = {"env": env_key} if env_key else {}
            rows = g.query(cypher, **params)
    except Exception as e:
        return {"incidents": [], "error": str(e)}
    return {"incidents": rows, "count": len(rows)}


@app.post("/mcp/call/{tool_name}")
async def mcp_call_tool(tool_name: str, req: _ToolCallRequest) -> dict[str, Any]:
    """Dispatch one tool invocation. Stateless — each call opens fresh
    backing-store handles. Fine at Phase 1 scale; Phase 2 introduces an
    app.state-scoped pool if latency matters."""
    from asil_core.llm import ModelRouter
    from asil_memory import (
        EpisodicStore,
        EpisodicStoreError,
        GraphStore,
        GraphStoreError,
        VectorStore,
        VectorStoreError,
    )

    from asil_api.mcp_tools import call_tool

    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except GraphStoreError as e:
        return {"error": f"neo4j unreachable: {e}"}

    vstore: VectorStore | None = None
    try:
        vstore = VectorStore()
        vstore.verify_connectivity()
    except VectorStoreError:
        # Some tools don't need the vector store; let the dispatcher decide.
        vstore = None

    estore: EpisodicStore | None = None
    try:
        estore = EpisodicStore(vector_store=vstore)
        estore.verify_connectivity()
        estore.apply_schema()
    except EpisodicStoreError:
        estore = None

    router = app.state.router if hasattr(app.state, "router") else ModelRouter.from_env()
    try:
        result = await call_tool(
            tool_name,
            req.arguments,
            graph_store=gstore,
            vector_store=vstore,
            router=router,
            episodic_store=estore,
        )
        return {"tool": tool_name, "result": result}
    except ValueError as e:
        return {"tool": tool_name, "error": str(e)}
    finally:
        gstore.close()
        if vstore is not None:
            vstore.close()
        if estore is not None:
            estore.close()
