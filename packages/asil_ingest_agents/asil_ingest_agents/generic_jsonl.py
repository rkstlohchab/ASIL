"""Generic JSONL transcript ingester.

For any agent whose history is a JSONL file with one message per line.
The user passes a `column_map` telling the parser which fields hold
the role and the text. Use this when ASIL doesn't ship a dedicated
parser for your agent — Aider, OpenHands, your own custom agent.

Example:

    asil ingest-transcripts generic-jsonl \\
        --path ~/.aider/chat.jsonl \\
        --role-key role --text-key content \\
        --source aider-transcript
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from asil_ingest_agents.base import IngestPlan, QAChunk, Turn


@dataclass(slots=True)
class GenericJsonlIngester:
    paths: list[Path]
    role_key: str = "role"
    text_key: str = "content"
    ts_key: str | None = "timestamp"
    user_label: str = "user"
    assistant_label: str = "assistant"
    source: str = "generic-jsonl-transcript"

    def plan(
        self,
        *,
        since: datetime | None = None,
        project: str | None = None,  # unused; here for CLI parity
        session: str | None = None,
    ) -> IngestPlan:
        chunks: list[QAChunk] = []
        seen_sessions: list[str] = []
        for p in self.paths:
            if not p.exists():
                continue
            if since is not None:
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                if mtime < since:
                    continue
            session_id = p.stem
            seen_sessions.append(session_id)
            turns = self._parse(p)
            chunks.extend(_pair(turns, session_id=session_id, source=self.source))
        if session:
            chunks = [c for c in chunks if c.session_id == session]
        return IngestPlan(source=self.source, sessions=seen_sessions, qa_chunks=chunks)

    def _parse(self, path: Path) -> list[Turn]:
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
                role_val = str(rec.get(self.role_key, "")).lower()
                if self.user_label in role_val:
                    role = "user"
                elif self.assistant_label in role_val:
                    role = "assistant"
                else:
                    continue
                text = rec.get(self.text_key)
                if isinstance(text, list):
                    text = "\n".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in text
                    )
                if not isinstance(text, str) or not text.strip():
                    continue
                import contextlib

                ts_raw = rec.get(self.ts_key) if self.ts_key else None
                ts: datetime | None = None
                if ts_raw:
                    with contextlib.suppress(ValueError):
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                turns.append(
                    Turn(role=role, text=text.strip(), ts=ts, message_id=str(rec.get("id") or ""))
                )
        return turns


def _pair(turns: list[Turn], *, session_id: str, source: str) -> list[QAChunk]:
    chunks: list[QAChunk] = []
    q: Turn | None = None
    responses: list[Turn] = []
    for t in turns:
        if t.role == "user":
            if q is not None:
                chunks.append(_close(q, responses, session_id, source))
            q = t
            responses = []
        elif t.role == "assistant" and q is not None:
            responses.append(t)
    if q is not None:
        chunks.append(_close(q, responses, session_id, source))
    return chunks


def _close(q: Turn, responses: list[Turn], session_id: str, source: str) -> QAChunk:
    resp = "\n\n".join(r.text for r in responses if r.text).strip() or "(no assistant response captured)"
    if len(resp) > 3000:
        resp = resp[:3000] + "\n…[truncated]"
    return QAChunk(
        question=q.text,
        assistant_response=resp,
        session_id=session_id,
        source=source,
        start_ts=q.ts,
        end_ts=responses[-1].ts if responses else q.ts,
        turn_ids=[t.message_id for t in [q, *responses] if t.message_id],
    )
