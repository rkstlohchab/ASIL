"""ASIL's MCP tool surface.

This is the layer that makes ASIL useful *to other agents*. Each tool is a
pure async function: input dict → output dict. The same handlers are exposed
two ways:

  - over HTTP via `apps.api.main`: `GET /mcp/tools` lists the schemas,
    `POST /mcp/call/{tool}` invokes one. This is what the CLI's smoke tests
    and any HTTP-based client (curl, Postman, custom integrations) use.
  - over native MCP stdio: planned for Phase 7 polish, when we ship the
    `asil-mcp` console script so Claude Code / Cursor / OpenHands can wire
    ASIL in as a first-class MCP server. The tool definitions below are the
    contract; only the transport changes.

Design rules:
  - Every tool is async so it can call ModelRouter / GraphStore / VectorStore
    without ceremony when needed.
  - Every tool returns JSON-safe primitives. No Pydantic models, no dataclass
    instances — the wire shape is what users see, and what tests check.
  - Every tool's payload includes a `confidence` block when the result is the
    outcome of reasoning (e.g. `ask`). Hard rule per CLAUDE.md.
  - Tools are read-only by default. Mutations (re-ingest, clear) belong in
    the CLI; agents don't get them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from asil_core.llm import ModelRouter
from asil_memory import (
    EpisodicStore,
    GraphStore,
    HybridRetriever,
    Memory,
    VectorStore,
)
from asil_reasoning import Verifier, score_verified_answer

# ---------------------------------------------------------------------------
# Tool catalog — public-facing JSON Schemas, paired with handler refs.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


TOOL_CATALOG: list[ToolSpec] = [
    ToolSpec(
        name="asil.search_code",
        description=(
            "Hybrid semantic + graph search for code. Returns top-K candidate "
            "functions/classes with citations. Use this when you have a natural-"
            "language description of what you're looking for."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query."},
                "repo_key": {
                    "type": ["string", "null"],
                    "description": "Optional repo scope (e.g. 'org/name'). Omit for cross-repo.",
                },
                "kind": {
                    "type": ["string", "null"],
                    "enum": ["function", "class", None],
                    "description": "Filter by node kind.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="asil.get_callers",
        description=(
            "List every Function that calls the given target (1-hop). Uses :CALLS "
            "edges resolved by the parser + import-aware heuristics. Returns "
            "[{qualified_name, file_path, line, derivation}]."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "qualified_name": {
                    "type": "string",
                    "description": "Fully-qualified function name, e.g. 'pkg.mod.Class.method'.",
                },
                "repo_key": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "required": ["qualified_name"],
        },
    ),
    ToolSpec(
        name="asil.get_dependencies",
        description=(
            "Inverse of get_callers — every Function that this one calls. "
            "Useful for tracing 'what does this function actually depend on?'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "qualified_name": {"type": "string"},
                "repo_key": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "required": ["qualified_name"],
        },
    ),
    ToolSpec(
        name="asil.who_owns",
        description=(
            "Phase 1 placeholder. Returns the containing file + (when available) "
            "the last commit author from git blame. Author resolution proper "
            "lands in Phase 2's commit-history ingestor."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path."},
                "repo_key": {"type": ["string", "null"]},
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="asil.commit_history",
        description=(
            "Phase 1 placeholder. The Commit/Author nodes wire up in Phase 2; "
            "this tool currently returns an empty list and a `not_yet_implemented` "
            "flag so callers can detect the stub."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "since": {"type": ["string", "null"]},
                "repo_key": {"type": ["string", "null"]},
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="asil.remember",
        description=(
            "Persist a conclusion to episodic memory. Most callers don't need this "
            "directly — `asil.ask` writes to memory automatically. Use this to "
            "record an out-of-band fact you want ASIL to recall later."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "repo_key": {"type": "string"},
                "question": {"type": "string"},
                "answer": {"type": "string"},
                "citations": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Optional list of {qualified_name, file_path, start_line, kind}.",
                    "default": [],
                },
            },
            "required": ["repo_key", "question", "answer"],
        },
    ),
    ToolSpec(
        name="asil.recall",
        description=(
            "Semantic search over episodic memory. Returns past conclusions whose "
            "questions are similar to the query. Useful when an agent wants to "
            "check whether ASIL has already reasoned about a topic."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "repo_key": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                "min_similarity": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 0.5,
                },
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="asil.forget",
        description=(
            "Hard-delete one memory by id. Idempotent. Use when ASIL persisted a "
            "wrong or stale conclusion."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Full UUID."},
            },
            "required": ["memory_id"],
        },
    ),
    ToolSpec(
        name="asil.find_causes",
        description=(
            "Phase 4 — temporal causality. Given an Incident id, return the "
            "ranked causal candidates (Deployments / MetricShifts / "
            "LogSignatures that occurred before it) with proximity-derived "
            "confidence + a human-readable derivation string. Reads the "
            ":PRECEDED edges produced by `TemporalLinker`; the same shape "
            "you'd get from `asil temporal causes <id> --read`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "incident_id": {"type": "string"},
                "min_confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 0.05,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            },
            "required": ["incident_id"],
        },
    ),
    ToolSpec(
        name="asil.ask",
        description=(
            "Highest-level tool. Embeds the question, runs the hybrid retriever, "
            "passes the top candidates to the reasoning LLM with a strict cite-"
            "everything system prompt, then runs a verifier pass that downgrades "
            "Confidence if any claim isn't backed by a citation. Returns "
            "{answer, confidence, citations, verifier}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "repo_key": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                "verify": {
                    "type": "boolean",
                    "default": True,
                    "description": "Run the second-pass claim verifier (adds one LLM call).",
                },
            },
            "required": ["question"],
        },
    ),
    ToolSpec(
        name="asil.replay_incident",
        description=(
            "Phase 5 — execution replay. Given an Incident id, return the "
            "full incident story: timeline, top causes, service cascade, "
            "state diff (before/after), and aggregated confidence. The same "
            "data `asil replay <id>` renders in the terminal. Reads "
            ":PRECEDED edges + runtime events from the graph; run "
            "`asil temporal link <env>` first to populate causal edges."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "incident_id": {"type": "string"},
                "causes_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 5,
                },
            },
            "required": ["incident_id"],
        },
    ),
    ToolSpec(
        name="asil.drift_check",
        description=(
            "Phase 6 — architecture drift. Given a repo key, compare the "
            "current dependency graph against a baseline (empty if none "
            "provided). Returns a list of DriftEvent objects describing "
            "new dependencies, removed dependencies, and boundary violations. "
            "Use this before merging changes to check for unexpected "
            "architectural shifts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "repo_key": {"type": "string"},
            },
            "required": ["repo_key"],
        },
    ),
    ToolSpec(
        name="asil.propose_fix",
        description=(
            "Phase 8 — constrained fix proposer. Given an incident id and a "
            "local repo root, generate a minimal unified diff that addresses "
            "the TOP causal candidate ASIL's deterministic linker identified. "
            "Read-only by default — does NOT apply the diff or run any tests. "
            "Set `record: true` to persist the proposal to the audit log "
            "(`asil_fix_audit` table). Use the CLI's `asil fix run` for the "
            "full sandboxed pipeline."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "incident_id": {"type": "string"},
                "repo_root": {
                    "type": "string",
                    "description": "Absolute path to the repo on disk.",
                },
                "repo_key": {
                    "type": ["string", "null"],
                    "description": "Optional graph repo key; defaults to local:<repo_root>.",
                },
                "record": {
                    "type": "boolean",
                    "default": False,
                    "description": "Persist the proposal to the audit log even without sandbox.",
                },
            },
            "required": ["incident_id", "repo_root"],
        },
    ),
]


def tool_catalog() -> list[dict[str, Any]]:
    """JSON-safe view of the catalog for `GET /mcp/tools`."""
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
        for t in TOOL_CATALOG
    ]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def search_code(
    payload: dict[str, Any],
    *,
    graph_store: GraphStore,
    vector_store: VectorStore,
    router: ModelRouter,
) -> dict[str, Any]:
    query = _required_str(payload, "query")
    repo_key = payload.get("repo_key")
    kind = payload.get("kind")
    limit = int(payload.get("limit", 10))

    retriever = HybridRetriever(
        graph_store=graph_store,
        vector_store=vector_store,
        embedder=router,
        final_limit=limit,
    )
    result = await retriever.retrieve(query, repo_key=repo_key, kind=kind)
    return {
        "query": query,
        "candidates": [_candidate_dict(c) for c in result.candidates],
        "confidence": _confidence_dict(result.confidence),
    }


async def get_callers(payload: dict[str, Any], *, graph_store: GraphStore) -> dict[str, Any]:
    qname = _required_str(payload, "qualified_name")
    repo_key = payload.get("repo_key")
    limit = int(payload.get("limit", 50))
    rows = graph_store.query(
        """
        MATCH (caller:Function)-[r:CALLS]->(callee:Function {qualified_name: $qname})
        WHERE ($repo IS NULL OR caller.repo_key = $repo)
        RETURN caller.qualified_name AS qualified_name,
               caller.file_path      AS file_path,
               caller.signature      AS signature,
               r.line                AS line,
               r.derivation          AS derivation
        ORDER BY caller.qualified_name
        LIMIT $limit
        """,
        qname=qname,
        repo=repo_key,
        limit=limit,
    )
    return {"target": qname, "callers": rows, "count": len(rows)}


async def get_dependencies(payload: dict[str, Any], *, graph_store: GraphStore) -> dict[str, Any]:
    qname = _required_str(payload, "qualified_name")
    repo_key = payload.get("repo_key")
    limit = int(payload.get("limit", 50))
    rows = graph_store.query(
        """
        MATCH (caller:Function {qualified_name: $qname})-[r:CALLS]->(callee:Function)
        WHERE ($repo IS NULL OR caller.repo_key = $repo)
        RETURN callee.qualified_name AS qualified_name,
               callee.file_path      AS file_path,
               callee.signature      AS signature,
               r.line                AS line,
               r.derivation          AS derivation
        ORDER BY callee.qualified_name
        LIMIT $limit
        """,
        qname=qname,
        repo=repo_key,
        limit=limit,
    )
    return {"caller": qname, "dependencies": rows, "count": len(rows)}


async def who_owns(payload: dict[str, Any], *, graph_store: GraphStore) -> dict[str, Any]:
    path = _required_str(payload, "path")
    repo_key = payload.get("repo_key")
    rows = graph_store.query(
        """
        MATCH (r:Repo)-[:CONTAINS]->(f:File {path: $path})
        WHERE ($repo IS NULL OR f.repo_key = $repo)
        RETURN r.key AS repo_key, r.spec AS spec, f.path AS path,
               f.language AS language, f.loc AS loc,
               f.module_name AS module_name
        LIMIT 1
        """,
        path=path,
        repo=repo_key,
    )
    if not rows:
        return {"path": path, "found": False, "note": "no File node with that path"}
    out = dict(rows[0])
    out["found"] = True
    # Phase 1 placeholder — Author resolution lands in Phase 2 alongside Commit nodes.
    out["author"] = None
    out["last_commit"] = None
    out["note"] = "author/commit resolution is Phase 2 work; returning file metadata only"
    return out


async def commit_history(payload: dict[str, Any]) -> dict[str, Any]:
    _required_str(payload, "path")
    return {
        "commits": [],
        "not_yet_implemented": True,
        "note": "Commit/Author nodes ship in Phase 2 (memory + commit ingestor).",
    }


async def find_causes(payload: dict[str, Any], *, graph_store: GraphStore) -> dict[str, Any]:
    """Phase 4 — temporal causality. Read-side tool: returns :PRECEDED edges
    already in the graph. Run `asil temporal link <env>` (CLI) or call the
    `TemporalLinker.link_env` API to populate them first."""
    incident_id = _required_str(payload, "incident_id")
    min_confidence = float(payload.get("min_confidence", 0.05))
    limit = int(payload.get("limit", 20))
    rows = graph_store.causes_for_incident(incident_id, min_confidence=min_confidence, limit=limit)
    return {
        "incident_id": incident_id,
        "causes": [
            {
                "cause_kind": r["cause_kind"],
                "confidence": round(float(r["confidence"]), 4),
                "delta_seconds": round(float(r["delta_seconds"]), 1),
                "derivation": r.get("derivation"),
                "strategy": r.get("strategy"),
                "cause_props": _scrub_neo4j_props(r["cause_props"]),
            }
            for r in rows
        ],
        "count": len(rows),
    }


def _scrub_neo4j_props(props: dict[str, Any]) -> dict[str, Any]:
    """Convert Neo4j-native DateTime/Date objects into ISO strings so the
    payload is JSON-serializable per the asil-mcp-tool contract."""
    out: dict[str, Any] = {}
    for k, v in props.items():
        out[k] = str(v) if v is not None and type(v).__name__ in {"DateTime", "Date", "Time"} else v
    return out


async def replay_incident(payload: dict[str, Any], *, graph_store: GraphStore) -> dict[str, Any]:
    """Phase 5 — execution replay. Returns the full incident story as JSON."""
    from asil_replay import ReplayEngine

    incident_id = _required_str(payload, "incident_id")
    causes_limit = int(payload.get("causes_limit", 5))

    engine = ReplayEngine(graph_store=graph_store)
    result = engine.replay(incident_id, causes_limit=causes_limit)
    if result is None:
        return {"error": f"incident {incident_id!r} not found", "incident_id": incident_id}

    # Serialize timeline
    timeline = [
        {
            "at": e.at,
            "kind": e.kind,
            "service": e.service,
            "description": e.description,
            "marker": e.marker,
        }
        for e in result.timeline
    ]

    # Serialize causes (already dict-shaped from causes_for_incident)
    causes = [
        {
            "cause_kind": c.get("cause_kind"),
            "confidence": round(float(c.get("confidence", 0)), 4),
            "delta_seconds": round(float(c.get("delta_seconds", 0)), 1),
            "derivation": c.get("derivation"),
            "strategy": c.get("strategy"),
            "cause_props": _scrub_neo4j_props(c.get("cause_props", {})),
        }
        for c in result.top_causes
    ]

    # Serialize cascade
    cascade = [
        {
            "service": s.service,
            "first_event_at": s.first_event_at,
            "first_event_kind": s.first_event_kind,
            "first_event_description": s.first_event_description,
        }
        for s in result.service_cascade
    ]

    # Serialize state diff
    state_diff = None
    if result.state_diff is not None:
        sd = result.state_diff
        state_diff = {
            "services_involved": sd.services_involved,
            "deployments_during": [
                {
                    "deployment_id": d.deployment_id,
                    "service": d.service,
                    "description": d.description,
                    "commit_sha": d.commit_sha,
                    "at": d.at,
                }
                for d in sd.deployments_during
            ],
            "metric_deltas": [
                {
                    "service": m.service,
                    "metric": m.metric,
                    "before": m.before,
                    "after": m.after,
                    "unit": m.unit,
                }
                for m in sd.metric_deltas
            ],
        }

    return {
        "incident_id": incident_id,
        "incident": _scrub_neo4j_props(result.incident),
        "summary_lines": result.summary_lines,
        "timeline": timeline,
        "top_causes": causes,
        "service_cascade": cascade,
        "state_diff": state_diff,
        "confidence": _confidence_dict(result.confidence),
    }


async def propose_fix(
    payload: dict[str, Any],
    *,
    graph_store: GraphStore,
    router: ModelRouter,
) -> dict[str, Any]:
    """Phase 8 — constrained fix proposer (read-only by default).

    Wraps `PatchGenerator.propose()`. When `record=true`, writes a no-op
    sandbox row to `asil_fix_audit` so the proposal is still tracked.
    The full propose -> apply -> test pipeline lives in the CLI; agents
    don't get a one-shot tool that runs untrusted patches.
    """
    from pathlib import Path as _Path

    from asil_fix import NoOpSandbox, PatchGenerator
    from asil_fix.audit import from_settings_or_none as _audit_or_none

    incident_id = _required_str(payload, "incident_id")
    repo_root = _required_str(payload, "repo_root")
    repo_key = payload.get("repo_key") or f"local:{_Path(repo_root).resolve()}"
    record = bool(payload.get("record", False))

    generator = PatchGenerator(router=router, graph_store=graph_store)
    try:
        proposal = await generator.propose(
            incident_id=incident_id,
            repo_root=repo_root,
            repo_key=repo_key,
        )
    except ValueError as exc:
        return {"error": str(exc), "incident_id": incident_id}

    audited_outcome: str | None = None
    if record:
        audit = _audit_or_none()
        if audit is not None:
            sandbox_result = NoOpSandbox().run(proposal, repo_root)
            audited_outcome = audit.record(proposal, sandbox_result).value

    return {
        "incident_id": proposal.incident_id,
        "summary": proposal.summary,
        "diff": proposal.diff,
        "affected_files": proposal.affected_files,
        "causal_chain": _scrub_neo4j_props(proposal.causal_chain),
        "confidence_score": proposal.confidence_score,
        "derivation": proposal.derivation,
        "model": proposal.model,
        "cost_usd": round(proposal.cost_usd, 6),
        "generated_at": proposal.generated_at.isoformat(),
        "audited_outcome": audited_outcome,
    }


async def drift_check(payload: dict[str, Any], *, graph_store: GraphStore) -> dict[str, Any]:
    """Phase 6 — architecture drift. Compares current graph vs empty baseline."""
    from asil_drift import BaselineSnapshot, DriftDetector

    repo_key = _required_str(payload, "repo_key")
    baseline = BaselineSnapshot(repo_key=repo_key)

    detector = DriftDetector(graph_store=graph_store)
    events = detector.detect(repo_key, baseline)

    return {
        "repo_key": repo_key,
        "drift_events": [
            {
                "kind": e.kind,
                "caller": e.caller,
                "callee": e.callee,
                "severity": e.severity,
                "description": e.description,
                "boundary_name": e.boundary_name,
            }
            for e in events
        ],
        "count": len(events),
    }


async def remember(
    payload: dict[str, Any],
    *,
    episodic_store: EpisodicStore,
    router: ModelRouter,
    profile_name: str,
) -> dict[str, Any]:
    """Out-of-band write to episodic memory. Caller supplies the conclusion;
    we embed the question + persist."""
    from asil_core import Confidence

    repo_key = _required_str(payload, "repo_key")
    question = _required_str(payload, "question")
    answer = _required_str(payload, "answer")
    citations = payload.get("citations") or []
    if not isinstance(citations, list):
        raise ValueError("'citations' must be a list")

    # Out-of-band remembers get a low-evidence baseline confidence so they
    # don't outrank verified `asil.ask` conclusions when recalled.
    conf = Confidence(
        score=0.5,
        evidence_count=0,
        retrieval_strength=0.0,
        causal_confidence=0.0,
        derivation=["out-of-band write via asil.remember"],
    )

    episodic_store.apply_schema()
    vec = (await router.embed([question]))[0]
    mem = episodic_store.remember(
        repo_key=repo_key,
        question=question,
        answer=answer,
        confidence=conf,
        citations=citations,
        model="(remember)",
        provider="(remember)",
        cost_usd=0.0,
        profile=profile_name,
        question_vector=vec,
    )
    return {"id": mem.id, "created_at": mem.created_at.isoformat()}


async def recall(
    payload: dict[str, Any],
    *,
    episodic_store: EpisodicStore,
    router: ModelRouter,
) -> dict[str, Any]:
    query = _required_str(payload, "query")
    repo_key = payload.get("repo_key")
    limit = int(payload.get("limit", 5))
    min_sim = float(payload.get("min_similarity", 0.5))

    vec = (await router.embed([query]))[0]
    hits = episodic_store.recall_similar(
        query_vector=vec,
        repo_key=repo_key,
        limit=limit,
        min_similarity=min_sim,
    )
    return {
        "query": query,
        "hits": [_memory_hit_dict(h.memory, h.similarity) for h in hits],
        "count": len(hits),
    }


async def forget(payload: dict[str, Any], *, episodic_store: EpisodicStore) -> dict[str, Any]:
    memory_id = _required_str(payload, "memory_id")
    removed = episodic_store.forget(memory_id)
    return {"memory_id": memory_id, "removed": removed}


async def ask(
    payload: dict[str, Any],
    *,
    graph_store: GraphStore,
    vector_store: VectorStore,
    router: ModelRouter,
) -> dict[str, Any]:
    question = _required_str(payload, "question")
    repo_key = payload.get("repo_key")
    limit = int(payload.get("limit", 8))
    run_verifier = bool(payload.get("verify", True))

    retriever = HybridRetriever(
        graph_store=graph_store,
        vector_store=vector_store,
        embedder=router,
        final_limit=limit,
    )
    result = await retriever.retrieve(question, repo_key=repo_key)
    if not result.candidates:
        return {
            "question": question,
            "answer": "No indexed code matched this question. Try a different phrasing or ingest the relevant repo.",
            "citations": [],
            "confidence": _confidence_dict(result.confidence),
            "verifier": None,
            "cost_usd": 0.0,
        }

    prompt = _build_ask_prompt(question, result)
    resp = await router.call(
        tier="reasoning",
        messages=[{"role": "user", "content": prompt}],
        system=_ASK_SYSTEM_PROMPT,
        max_tokens=900,
        temperature=0.1,
    )

    confidence = result.confidence
    verifier_payload: dict[str, Any] | None = None
    verifier_cost = 0.0
    if run_verifier:
        verifier = Verifier(router=router)
        vr = await verifier.verify(question, resp.text, result.candidates)
        verifier_cost = vr.cost_usd
        if not vr.skipped:
            confidence = score_verified_answer(result.confidence, vr)
        verifier_payload = {
            "skipped": vr.skipped,
            "skip_reason": vr.skip_reason,
            "unsupported_count": vr.unsupported_count,
            "claims": [
                {
                    "claim": c.claim,
                    "supported": c.supported,
                    "citation": c.citation,
                    "reason": c.reason,
                }
                for c in vr.claims
            ],
        }

    citations = [
        {
            "qualified_name": c.qualified_name,
            "file_path": c.file_path,
            "start_line": c.start_line,
            "kind": c.kind,
            "score": round(c.score, 4),
        }
        for c in result.candidates
    ]
    return {
        "question": question,
        "answer": resp.text,
        "citations": citations,
        "confidence": _confidence_dict(confidence),
        "verifier": verifier_payload,
        "cost_usd": resp.cost_usd + verifier_cost,
        "model": resp.model,
        "provider": resp.provider,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_TOOL_HANDLERS: dict[str, str] = {t.name: t.name for t in TOOL_CATALOG}


async def call_tool(
    name: str,
    payload: dict[str, Any],
    *,
    graph_store: GraphStore,
    vector_store: VectorStore | None,
    router: ModelRouter | None,
    episodic_store: EpisodicStore | None = None,
) -> dict[str, Any]:
    """Route a tool call by name. Validates name + required deps for that tool."""
    if name not in _TOOL_HANDLERS:
        return {"error": f"unknown tool: {name!r}", "available": list(_TOOL_HANDLERS)}

    if name == "asil.search_code":
        _need(vector_store, router, name=name)
        return await search_code(
            payload,
            graph_store=graph_store,
            vector_store=vector_store,
            router=router,  # type: ignore[arg-type]
        )
    if name == "asil.get_callers":
        return await get_callers(payload, graph_store=graph_store)
    if name == "asil.get_dependencies":
        return await get_dependencies(payload, graph_store=graph_store)
    if name == "asil.who_owns":
        return await who_owns(payload, graph_store=graph_store)
    if name == "asil.commit_history":
        return await commit_history(payload)
    if name == "asil.find_causes":
        return await find_causes(payload, graph_store=graph_store)
    if name == "asil.ask":
        _need(vector_store, router, name=name)
        return await ask(
            payload,
            graph_store=graph_store,
            vector_store=vector_store,
            router=router,  # type: ignore[arg-type]
        )
    if name == "asil.remember":
        _need(episodic_store, router, name=name)
        return await remember(
            payload,
            episodic_store=episodic_store,  # type: ignore[arg-type]
            router=router,  # type: ignore[arg-type]
            profile_name=getattr(router, "active_profile_name", "(remember)"),
        )
    if name == "asil.recall":
        _need(episodic_store, router, name=name)
        return await recall(
            payload,
            episodic_store=episodic_store,  # type: ignore[arg-type]
            router=router,  # type: ignore[arg-type]
        )
    if name == "asil.forget":
        _need(episodic_store, name=name)
        return await forget(payload, episodic_store=episodic_store)  # type: ignore[arg-type]
    if name == "asil.replay_incident":
        return await replay_incident(payload, graph_store=graph_store)
    if name == "asil.drift_check":
        return await drift_check(payload, graph_store=graph_store)
    if name == "asil.propose_fix":
        _need(router, name=name)
        return await propose_fix(
            payload,
            graph_store=graph_store,
            router=router,  # type: ignore[arg-type]
        )
    return {"error": f"handler missing for {name!r}"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _required_str(payload: dict[str, Any], key: str) -> str:
    v = payload.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{key!r} is required and must be a non-empty string")
    return v


def _need(*deps: Any, name: str) -> None:
    if any(d is None for d in deps):
        raise RuntimeError(
            f"tool {name!r} requires the vector store and LLM router; "
            "they were not available in this server context."
        )


def _memory_hit_dict(m: Memory, similarity: float) -> dict[str, Any]:
    return {
        "id": m.id,
        "similarity": round(similarity, 4),
        "repo_key": m.repo_key,
        "question": m.question,
        "answer": m.answer,
        "confidence": {
            "score": round(m.confidence.score, 4),
            "evidence_count": m.confidence.evidence_count,
        },
        "citations": m.citations,
        "verifier_unsupported": m.verifier_unsupported,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "model": m.model,
        "provider": m.provider,
    }


def _candidate_dict(c: Any) -> dict[str, Any]:
    return {
        "qualified_name": c.qualified_name,
        "name": c.name,
        "kind": c.kind,
        "file_path": c.file_path,
        "start_line": c.start_line,
        "end_line": c.end_line,
        "score": round(c.score, 4),
        "source": c.source,
        "signature": c.signature,
        "docstring": c.docstring,
        "parent_class": c.parent_class,
    }


def _confidence_dict(conf: Any) -> dict[str, Any]:
    return {
        "score": round(conf.score, 4),
        "evidence_count": conf.evidence_count,
        "retrieval_strength": round(conf.retrieval_strength, 4),
        "causal_confidence": round(conf.causal_confidence, 4),
        "derivation": list(conf.derivation),
    }


_ASK_SYSTEM_PROMPT = (
    "You are ASIL, the engineering intelligence layer for this codebase. "
    "Answer the user's question using ONLY the code snippets provided. "
    "Rules:\n"
    "  1. Cite every concrete claim with the file:line of the supporting snippet, like (graph_store.py:116).\n"
    "  2. If the snippets don't actually answer the question, say so plainly — do not invent.\n"
    "  3. Prefer short, direct prose. Use a fenced ```py``` block only when quoting code is the clearest answer.\n"
    "  4. Never reference 'the snippets' or 'the context' in your response — speak as if you simply know the code.\n"
    "  5. If the answer requires details not present, end with one sentence on what additional evidence would resolve it."
)


def _build_ask_prompt(question: str, result: Any) -> str:
    """Same shape as the CLI's ask prompt — kept here so the MCP path and the
    CLI path stay consistent. If we change one, update the other."""
    lines = [f"Question: {question}", "", "Code snippets retrieved (most relevant first):"]
    for i, c in enumerate(result.candidates, 1):
        header = f"[{i}] {c.qualified_name}  —  {c.file_path}:{c.start_line}"
        if c.signature:
            header += f"  signature: {c.signature}"
        lines.append("")
        lines.append(header)
        if c.docstring:
            lines.append(f"  doc: {c.docstring.strip()[:300]}")
        if c.text:
            snippet = c.text if len(c.text) <= 1200 else c.text[:1200] + "\n  …"
            lines.append("```")
            lines.append(snippet)
            lines.append("```")
    lines.extend(["", "Answer the question now. Cite with file:line as specified."])
    return "\n".join(lines)


__all__ = [
    "TOOL_CATALOG",
    "ToolSpec",
    "ask",
    "call_tool",
    "commit_history",
    "drift_check",
    "get_callers",
    "get_dependencies",
    "replay_incident",
    "search_code",
    "tool_catalog",
    "who_owns",
]


# Acknowledge json import for serialization callers that build payloads;
# kept eager so MyPy/test discovery don't lose the symbol on dead-code passes.
_ = json
