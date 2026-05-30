"""Dump live FastAPI responses to apps/web/public/snapshot/ for the
static-export dashboard.

Run after `asil ingest .` + `asil postmortem ingest <yamls>` against a
running asil-api on localhost. Every REST endpoint the dashboard hits
gets saved, plus one MCP-tool response per page. For per-incident
tools (replay_incident, find_causes) we emit one fixture per real
incident id so the UI can drill into any of them.

Invoked by .github/workflows/asil-report.yml. Idempotent — overwrites
the fixtures it owns and leaves the rest alone.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, request

DEFAULT_BASE = "http://localhost:8000"
SNAPSHOT_DIR = Path("apps/web/public/snapshot")


def _http_get(base: str, path: str) -> Any:
    req = request.Request(f"{base}{path}")
    with request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _http_post(base: str, path: str, body: dict[str, Any]) -> Any:
    req = request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def _call_tool(base: str, name: str, args: dict[str, Any]) -> Any:
    """Unwrap the {tool, result, error} envelope so the saved fixture
    matches what the dashboard's api.callTool() returns (the `result`)."""
    resp = _http_post(base, f"/mcp/call/{name}", {"arguments": args})
    if resp.get("error"):
        raise RuntimeError(f"{name}: {resp['error']}")
    return resp.get("result")


def _write(rel: str, body: Any) -> None:
    out = SNAPSHOT_DIR / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2, default=str) + "\n")
    print(f"  wrote {out.relative_to('.')}")


def _safe_id_for_path(value: str) -> str:
    """Incident IDs like `INC-2026-03-19-dns-misconfig-checkout` are
    filename-safe by convention. Defensively replace any path separators."""
    return value.replace("/", "_").replace("\\", "_")


def wait_for_api(base: str, timeout_seconds: int = 60) -> None:
    deadline = time.time() + timeout_seconds
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            _http_get(base, "/health")
            return
        except (error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
            time.sleep(1)
    raise RuntimeError(f"api never came up at {base}: {last_err}")


def snapshot_rest(base: str) -> tuple[list[str], list[str]]:
    """Snapshot the REST endpoints. Returns (repo_keys, incident_ids)
    so the caller can drive the per-entity MCP tool snapshots."""
    print("REST endpoints:")
    stats = _http_get(base, "/dashboard/stats")
    _write("dashboard/stats.json", stats)
    repo_keys: list[str] = [r["key"] for r in stats.get("repos", []) if r.get("key")]

    _write("health.json", _http_get(base, "/health"))
    _write("dashboard/memory.json", _http_get(base, "/dashboard/memory?days=30&top_n=10"))
    _write("dashboard/cost.json", _http_get(base, "/dashboard/cost?days=30"))
    _write("mcp/tools.json", _http_get(base, "/mcp/tools"))

    incidents = _http_get(base, "/incidents")
    _write("incidents.json", incidents)
    incident_ids: list[str] = [
        i["incident_id"] for i in incidents.get("incidents", []) if i.get("incident_id")
    ]
    return repo_keys, incident_ids


def snapshot_mcp_tools(base: str, repo_keys: list[str], incident_ids: list[str]) -> None:
    print("MCP tools:")

    # asil.recall — generic query so the fixture always has something to show
    try:
        _write(
            "mcp/asil.recall.json",
            _call_tool(base, "asil.recall", {"query": "hybrid retrieval", "limit": 20}),
        )
    except Exception as exc:
        print(f"  asil.recall skipped: {exc}")
        _write("mcp/asil.recall.json", {"hits": [], "count": 0})

    # asil.drift_check — first ingested repo
    if repo_keys:
        try:
            _write(
                "mcp/asil.drift_check.json",
                _call_tool(base, "asil.drift_check", {"repo_key": repo_keys[0]}),
            )
        except Exception as exc:
            print(f"  asil.drift_check skipped: {exc}")
            _write(
                "mcp/asil.drift_check.json",
                {"repo_key": repo_keys[0], "drift_events": [], "count": 0},
            )
    else:
        _write("mcp/asil.drift_check.json", {"repo_key": None, "drift_events": [], "count": 0})

    # asil.ask — best-effort. Needs LLM creds + budget > 0. If unavailable,
    # write a placeholder so the page still renders.
    try:
        _write(
            "mcp/asil.ask.json",
            _call_tool(
                base,
                "asil.ask",
                {"question": "How does the LLM router pick a provider per tier?"},
            ),
        )
    except Exception as exc:
        print(f"  asil.ask skipped (likely no LLM creds): {exc}")
        _write(
            "mcp/asil.ask.json",
            {
                "question": "How does the LLM router pick a provider per tier?",
                "answer": None,
                "candidates": [],
                "confidence": {
                    "score": 0.0,
                    "evidence_count": 0,
                    "retrieval_strength": 0.0,
                    "causal_confidence": 0.0,
                    "derivation": ["asil.ask is disabled in this deployment (no LLM credentials)"],
                },
                "verifier": {"claims": [], "unsupported_count": 0},
                "memory_hits": [],
            },
        )

    # Per-incident: replay_incident + find_causes — one fixture each
    print(f"Per-incident MCP fixtures ({len(incident_ids)} incidents):")
    for iid in incident_ids:
        safe = _safe_id_for_path(iid)
        try:
            _write(
                f"mcp/asil.replay_incident__{safe}.json",
                _call_tool(base, "asil.replay_incident", {"incident_id": iid}),
            )
        except Exception as exc:
            print(f"  replay_incident {iid} skipped: {exc}")
        try:
            _write(
                f"mcp/asil.find_causes__{safe}.json",
                _call_tool(base, "asil.find_causes", {"incident_id": iid}),
            )
        except Exception as exc:
            print(f"  find_causes {iid} skipped: {exc}")

    # Default fixtures (no-args path) — let the dashboard fall back to
    # the first incident when called without an id.
    if incident_ids:
        first = _safe_id_for_path(incident_ids[0])
        replay = SNAPSHOT_DIR / f"mcp/asil.replay_incident__{first}.json"
        causes = SNAPSHOT_DIR / f"mcp/asil.find_causes__{first}.json"
        if replay.exists():
            _write("mcp/asil.replay_incident.json", json.loads(replay.read_text()))
        if causes.exists():
            _write("mcp/asil.find_causes.json", json.loads(causes.read_text()))

    # Index file used by Next.js generateStaticParams to know which
    # incident routes to pre-render.
    _write("incident_ids.json", incident_ids)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE, help="asil-api base URL")
    parser.add_argument(
        "--out", default=str(SNAPSHOT_DIR), help="output directory (relative to cwd)"
    )
    args = parser.parse_args()

    global SNAPSHOT_DIR
    SNAPSHOT_DIR = Path(args.out)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"snapshotting dashboard fixtures from {args.base} → {SNAPSHOT_DIR}")
    wait_for_api(args.base)
    repo_keys, incident_ids = snapshot_rest(args.base)
    snapshot_mcp_tools(args.base, repo_keys, incident_ids)
    print(f"\nsnapshot complete: {len(repo_keys)} repo(s), {len(incident_ids)} incident(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
