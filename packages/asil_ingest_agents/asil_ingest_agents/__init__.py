"""Transcript ingesters — read each AI coding agent's local conversation
files, extract question/conclusion pairs, write them into ASIL's episodic
memory tagged with the agent of origin.

Once any agent's transcripts are ingested, every other MCP-speaking agent
on the same Postgres can recall those conclusions — that's the cross-IDE
context handoff the Medium post is about.
"""

from asil_ingest_agents.base import (
    IngestPlan,
    IngestResult,
    QAChunk,
    TranscriptIngester,
    Turn,
)
from asil_ingest_agents.claude_code import ClaudeCodeIngester, find_claude_code_sessions
from asil_ingest_agents.cursor import CursorIngester, find_cursor_workspaces
from asil_ingest_agents.generic_jsonl import GenericJsonlIngester
from asil_ingest_agents.watch import WatchTick, run_watch_loop

__all__ = [
    "ClaudeCodeIngester",
    "CursorIngester",
    "GenericJsonlIngester",
    "IngestPlan",
    "IngestResult",
    "QAChunk",
    "TranscriptIngester",
    "Turn",
    "WatchTick",
    "find_claude_code_sessions",
    "find_cursor_workspaces",
    "run_watch_loop",
]
