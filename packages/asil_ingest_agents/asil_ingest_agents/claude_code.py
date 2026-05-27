"""Claude Code transcript ingester.

Claude Code persists every session as a JSONL file under
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`. Each line is a
typed record — `user`, `assistant`, `file-history-snapshot`,
`attachment`, etc. — wrapping the message and provenance.

The pairing rule we use to extract Q/A chunks:

* A **real** user turn is `type=user` whose content is either a string
  or a list of `text` blocks only. User records whose content carries
  `tool_result` blocks are synthetic (Claude Code replaying tool output
  back to the model) and get skipped.
* Each real user turn opens a new `QAChunk`. Every subsequent
  `type=assistant` text block until the next real user turn is
  concatenated into that chunk's response. `tool_use` / `thinking`
  blocks are skipped — they aren't user-visible conclusions.

This produces ~one chunk per actual question the human typed. The
Phase 9.2 write-time dedupe path then folds near-duplicates so a
50-turn session usually settles into 5-10 memories.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from asil_ingest_agents.base import IngestPlan, QAChunk, Turn

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
SOURCE_TAG = "claude-code-transcript"


@dataclass(slots=True)
class _SessionFile:
    path: Path
    session_id: str
    project_dir: str
    mtime: datetime


def _decode_project_dir(encoded: str) -> str:
    """Claude Code encodes the project's absolute path by replacing `/`
    with `-`. We invert the mapping so the result matches the original
    directory format; doesn't disambiguate genuine hyphens vs slashes but
    is good enough for human-readable provenance."""
    return "/" + encoded.lstrip("-").replace("-", "/")


def find_claude_code_sessions(
    *,
    root: Path | None = None,
    project: str | None = None,
    since: datetime | None = None,
) -> list[_SessionFile]:
    """Walk `~/.claude/projects/` (or `root`) and return the JSONL session
    files that match the filters. `project` is matched as a substring
    against the decoded project path so callers can pass either the full
    cwd or any unique suffix."""
    base = root if root is not None else CLAUDE_PROJECTS_DIR
    out: list[_SessionFile] = []
    if not base.exists():
        return out
    for proj_dir in sorted(base.iterdir()):
        if not proj_dir.is_dir():
            continue
        decoded = _decode_project_dir(proj_dir.name)
        if project and project not in decoded:
            continue
        for jsonl in sorted(proj_dir.glob("*.jsonl")):
            mtime = datetime.fromtimestamp(jsonl.stat().st_mtime)
            if since is not None and mtime < since:
                continue
            out.append(
                _SessionFile(
                    path=jsonl,
                    session_id=jsonl.stem,
                    project_dir=decoded,
                    mtime=mtime,
                )
            )
    return out


def _extract_text_blocks(content: Any) -> str | None:
    """Pull all `text` blocks out of a Claude message body. Skip
    `tool_use`, `tool_result`, `thinking`, and any other block types
    that don't carry user-facing prose. Returns None for empty results."""
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                t = block.get("text", "")
                if t and t.strip():
                    chunks.append(t.strip())
        if chunks:
            return "\n".join(chunks)
    return None


def _is_real_user_turn(record: dict[str, Any]) -> bool:
    """User records whose content is *only* `tool_result` blocks are
    synthetic (Claude Code feeds tool output back to itself as 'user'
    messages). We want only the records where the human actually typed
    something."""
    if record.get("type") != "user":
        return False
    msg = record.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return True
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and (block.get("text") or "").strip()
            ):
                return True
    return False


def parse_session(path: Path) -> list[Turn]:
    """Read a single JSONL transcript into a normalised `Turn` list."""
    turns: list[Turn] = []
    with path.open() as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = rec.get("type")
            if t == "user":
                if not _is_real_user_turn(rec):
                    continue
                text = _extract_text_blocks((rec.get("message") or {}).get("content"))
                if text is None:
                    continue
                turns.append(
                    Turn(
                        role="user",
                        text=text,
                        ts=_parse_ts(rec.get("timestamp")),
                        message_id=rec.get("uuid"),
                        extra={"sessionId": rec.get("sessionId")},
                    )
                )
            elif t == "assistant":
                text = _extract_text_blocks((rec.get("message") or {}).get("content"))
                if text is None:
                    continue
                turns.append(
                    Turn(
                        role="assistant",
                        text=text,
                        ts=_parse_ts(rec.get("timestamp")),
                        message_id=rec.get("uuid"),
                        extra={"sessionId": rec.get("sessionId")},
                    )
                )
            # Other types (file-history-snapshot, attachment, etc.) are skipped.
    return turns


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def chunk_into_qa(
    turns: list[Turn],
    *,
    session_id: str,
    max_response_chars: int = 3000,
) -> list[QAChunk]:
    """Walk turns; each real user turn opens a QAChunk. The chunk closes
    on the next user turn (or end of stream). Assistant text in between
    is concatenated; over-long responses are tail-truncated."""
    chunks: list[QAChunk] = []
    current_q: Turn | None = None
    current_responses: list[Turn] = []
    for t in turns:
        if t.role == "user":
            if current_q is not None:
                chunks.append(_close_chunk(current_q, current_responses, session_id, max_response_chars))
            current_q = t
            current_responses = []
        elif t.role == "assistant" and current_q is not None:
            current_responses.append(t)
    if current_q is not None:
        chunks.append(_close_chunk(current_q, current_responses, session_id, max_response_chars))
    return chunks


def _close_chunk(
    q: Turn,
    responses: list[Turn],
    session_id: str,
    max_chars: int,
) -> QAChunk:
    resp = "\n\n".join(r.text for r in responses if r.text).strip()
    if not resp:
        resp = "(no assistant response captured)"
    if len(resp) > max_chars:
        resp = resp[:max_chars] + "\n…[truncated]"
    return QAChunk(
        question=q.text,
        assistant_response=resp,
        session_id=session_id,
        source=SOURCE_TAG,
        start_ts=q.ts,
        end_ts=responses[-1].ts if responses else q.ts,
        turn_ids=[t.message_id for t in [q, *responses] if t.message_id],
    )


@dataclass(slots=True)
class ClaudeCodeIngester:
    """Plan + execute the Claude Code transcript ingestion.

    Execution is in the CLI (`asil ingest-transcripts claude-code`) which
    has access to the LLM router for optional summarisation and the
    episodic store for the writes. This class is intentionally just the
    file-walking + parsing + chunking layer so it's easy to unit-test
    without spinning up Postgres or an LLM."""

    root: Path | None = None
    source: str = SOURCE_TAG

    def plan(
        self,
        *,
        since: datetime | None = None,
        project: str | None = None,
        session: str | None = None,
        max_chunks_per_session: int = 200,
    ) -> IngestPlan:
        sessions = find_claude_code_sessions(
            root=self.root, project=project, since=since
        )
        if session:
            sessions = [s for s in sessions if s.session_id == session]
        chunks: list[QAChunk] = []
        session_ids: list[str] = []
        for sf in sessions:
            session_ids.append(sf.session_id)
            try:
                turns = parse_session(sf.path)
            except Exception:
                continue
            qa = chunk_into_qa(turns, session_id=sf.session_id)
            if len(qa) > max_chunks_per_session:
                qa = qa[-max_chunks_per_session:]
            chunks.extend(qa)
        return IngestPlan(source=self.source, sessions=session_ids, qa_chunks=chunks)
