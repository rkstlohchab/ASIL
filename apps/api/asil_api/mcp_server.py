"""ASIL MCP server stub.

MCP is the protocol that lets coding agents (Claude Code, Cursor, OpenHands)
call ASIL as a tool. Phase 0 ships an empty server — capabilities are added
phase-by-phase:

  • Phase 1 adds: search_code, get_callers, get_dependencies, who_owns, commit_history
  • Phase 2 adds: remember, recall, forget
  • Phase 4 adds: find_causes, time_window_query
  • Phase 5 adds: replay_incident, cascade
  • Phase 6 adds: drift_check

We deliberately do NOT depend on the `mcp` package yet — that's a Phase 1
concern. This module exists so the path is reserved and the contract is
documented.
"""

from __future__ import annotations

from typing import Any


def list_tools() -> list[dict[str, Any]]:
    """Phase 0: no tools yet. Returns [] so clients can connect successfully."""
    return []


def server_info() -> dict[str, Any]:
    return {
        "name": "asil",
        "version": "0.0.1",
        "description": "ASIL — Engineering Intelligence Infrastructure (MCP server)",
        "tools_available": len(list_tools()),
        "phase": 0,
    }
