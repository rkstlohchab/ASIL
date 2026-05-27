"""Shared types + the `TranscriptIngester` protocol that every per-agent
parser implements.

The pipeline every parser drives is the same:

    transcript file → list[Turn] → list[QAChunk] → summariser → EpisodicStore.remember()

A `Turn` is one user/assistant message. A `QAChunk` is the smallest
unit ASIL stores: a single user question + the assistant's distilled
conclusion, plus the metadata needed to attribute the memory back to
the source session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(slots=True)
class Turn:
    """One message in an agent transcript. Parsers normalise their native
    formats into this shape."""

    role: str  # "user" / "assistant" / "tool" / "system"
    text: str
    ts: datetime | None = None
    message_id: str | None = None
    # Anything the parser wants to preserve for downstream attribution
    # (e.g. file paths the agent edited, tool calls made).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class QAChunk:
    """One user question + the assistant's response, ready to be summarised
    and stored as an episodic memory."""

    question: str
    assistant_response: str
    session_id: str
    source: str  # e.g. "claude-code-transcript", "cursor-transcript"
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    turn_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IngestPlan:
    """What an ingester *would* do without --dry-run. Returned by
    `plan()` so the CLI can show a preview before committing."""

    source: str
    sessions: list[str]
    qa_chunks: list[QAChunk]


@dataclass(slots=True)
class IngestResult:
    """What actually happened — used by the CLI to print the summary
    table."""

    source: str
    chunks_seen: int
    memories_written: int
    memories_folded: int
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.source}: {self.chunks_seen} chunks → "
            f"{self.memories_written} new memories, "
            f"{self.memories_folded} folded into existing"
        )


class TranscriptIngester(Protocol):
    """Every per-agent ingester implements this protocol."""

    source: str  # e.g. "claude-code-transcript"

    def plan(
        self,
        *,
        since: datetime | None = None,
        project: str | None = None,
        session: str | None = None,
    ) -> IngestPlan:
        """Walk local transcript files; return what would be ingested."""
        ...
