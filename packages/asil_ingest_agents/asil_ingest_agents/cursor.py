"""Cursor transcript ingester.

Cursor stores per-workspace state (including chat history) in
SQLite under platform-dependent paths:

  macOS:   ~/Library/Application Support/Cursor/User/workspaceStorage/<ws-id>/state.vscdb
  Linux:   ~/.config/Cursor/User/workspaceStorage/<ws-id>/state.vscdb
  Windows: %APPDATA%/Cursor/User/workspaceStorage/<ws-id>/state.vscdb

The `state.vscdb` is a key/value store (`ItemTable` (`key TEXT, value BLOB)`)
where chat blobs are stored under one of several known keys that have
changed across Cursor versions:

  workbench.panel.aichat.view.aichat.chatdata
  aichat.chat-data
  composer.chat-data
  cursor.aiChat.state

The blob is JSON. The schema inside the JSON has also drifted between
versions; we extract what we can and skip what we can't recognise rather
than fail hard. If Cursor changes its schema and the parser stops
returning chunks, the user gets a clear log line — not a silent miss.
"""

from __future__ import annotations

import contextlib
import json
import platform
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from asil_ingest_agents.base import IngestPlan, QAChunk, Turn

SOURCE_TAG = "cursor-transcript"

_KNOWN_KEYS = (
    "workbench.panel.aichat.view.aichat.chatdata",
    "aichat.chat-data",
    "composer.chat-data",
    "cursor.aiChat.state",
)


def cursor_storage_root() -> Path:
    """Best-guess location of Cursor's workspaceStorage on this OS."""
    system = platform.system()
    if system == "Darwin":
        return (
            Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"
        )
    if system == "Linux":
        return Path.home() / ".config" / "Cursor" / "User" / "workspaceStorage"
    if system == "Windows":
        import os

        return (
            Path(os.environ.get("APPDATA", str(Path.home())))
            / "Cursor"
            / "User"
            / "workspaceStorage"
        )
    # Fallback — at least don't crash.
    return Path.home() / ".cursor" / "workspaceStorage"


@dataclass(slots=True)
class _Workspace:
    db_path: Path
    workspace_id: str
    mtime: datetime


def find_cursor_workspaces(
    *,
    root: Path | None = None,
    since: datetime | None = None,
) -> list[_Workspace]:
    base = root if root is not None else cursor_storage_root()
    if not base.exists():
        return []
    out: list[_Workspace] = []
    for ws_dir in sorted(base.iterdir()):
        if not ws_dir.is_dir():
            continue
        db = ws_dir / "state.vscdb"
        if not db.exists():
            continue
        mtime = datetime.fromtimestamp(db.stat().st_mtime)
        if since is not None and mtime < since:
            continue
        out.append(_Workspace(db_path=db, workspace_id=ws_dir.name, mtime=mtime))
    return out


def _read_chat_blobs(db_path: Path) -> list[dict]:
    """Open the SQLite store read-only and pull out every known chat key."""
    blobs: list[dict] = []
    try:
        # Read-only URI handle so we never hold a writer lock on Cursor's db.
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error:
        return blobs
    try:
        cur = conn.cursor()
        with contextlib.suppress(sqlite3.Error):
            placeholders = ",".join(["?"] * len(_KNOWN_KEYS))
            cur.execute(
                f"SELECT key, value FROM ItemTable WHERE key IN ({placeholders})",
                _KNOWN_KEYS,
            )
            for _key, value in cur.fetchall():
                try:
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="ignore")
                    blobs.append(json.loads(value))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return blobs


def _extract_turns(blob: dict) -> list[Turn]:
    """Best-effort traversal of Cursor's chat JSON. The schema has shipped
    in several shapes; we recognise the common ones and bail quietly on
    the rest."""
    turns: list[Turn] = []

    # Shape A: top-level list of `tabs`, each with `bubbles` / `messages`.
    tabs = blob.get("tabs") or blob.get("chats") or []
    if isinstance(tabs, list):
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            messages = tab.get("bubbles") or tab.get("messages") or []
            turns.extend(
                _turns_from_messages(
                    messages, session_id=str(tab.get("id") or tab.get("tabId") or "")
                )
            )

    # Shape B: top-level `messages` directly.
    messages = blob.get("messages")
    if isinstance(messages, list):
        turns.extend(_turns_from_messages(messages, session_id=str(blob.get("id") or "")))

    return turns


def _turns_from_messages(messages: list, *, session_id: str) -> list[Turn]:
    out: list[Turn] = []
    if not isinstance(messages, list):
        return out
    for m in messages:
        if not isinstance(m, dict):
            continue
        role_raw = m.get("type") or m.get("role") or m.get("kind")
        if not role_raw:
            continue
        role = (
            "user"
            if "user" in str(role_raw).lower()
            else (
                "assistant"
                if any(k in str(role_raw).lower() for k in ("ai", "assistant", "bot"))
                else None
            )
        )
        if role is None:
            continue
        # Text can be in `text`, `content`, `markdown`, `richText`, ...
        text = m.get("text") or m.get("content") or m.get("markdown") or m.get("richText") or ""
        if isinstance(text, list):
            # Sometimes content is a list of {type:'text', text:'…'} blocks.
            joined = []
            for block in text:
                if isinstance(block, dict) and block.get("text"):
                    joined.append(block["text"])
                elif isinstance(block, str):
                    joined.append(block)
            text = "\n".join(joined)
        if not isinstance(text, str) or not text.strip():
            continue
        out.append(
            Turn(
                role=role,
                text=text.strip(),
                message_id=str(m.get("id") or m.get("bubbleId") or ""),
                extra={"sessionId": session_id},
            )
        )
    return out


def _chunk(turns: list[Turn], *, session_id: str) -> list[QAChunk]:
    """Same pairing rule as the Claude Code parser: one chunk per user
    turn, accumulate following assistant text until the next user turn."""
    chunks: list[QAChunk] = []
    q: Turn | None = None
    responses: list[Turn] = []
    for t in turns:
        if t.role == "user":
            if q is not None:
                chunks.append(_close(q, responses, session_id))
            q = t
            responses = []
        elif t.role == "assistant" and q is not None:
            responses.append(t)
    if q is not None:
        chunks.append(_close(q, responses, session_id))
    return chunks


def _close(q: Turn, responses: list[Turn], session_id: str) -> QAChunk:
    resp = (
        "\n\n".join(r.text for r in responses if r.text).strip()
        or "(no assistant response captured)"
    )
    if len(resp) > 3000:
        resp = resp[:3000] + "\n…[truncated]"
    return QAChunk(
        question=q.text,
        assistant_response=resp,
        session_id=session_id,
        source=SOURCE_TAG,
        turn_ids=[t.message_id for t in [q, *responses] if t.message_id],
    )


@dataclass(slots=True)
class CursorIngester:
    root: Path | None = None
    source: str = SOURCE_TAG

    def plan(
        self,
        *,
        since: datetime | None = None,
        workspace: str | None = None,
        session: str | None = None,  # accepted for CLI parity; matches Cursor tab id
    ) -> IngestPlan:
        workspaces = find_cursor_workspaces(root=self.root, since=since)
        if workspace:
            workspaces = [w for w in workspaces if workspace in w.workspace_id]
        chunks: list[QAChunk] = []
        session_ids: list[str] = []
        for ws in workspaces:
            session_ids.append(ws.workspace_id)
            for blob in _read_chat_blobs(ws.db_path):
                turns = _extract_turns(blob)
                for c in _chunk(turns, session_id=ws.workspace_id):
                    if session and c.session_id != session:
                        continue
                    chunks.append(c)
        return IngestPlan(source=self.source, sessions=session_ids, qa_chunks=chunks)
