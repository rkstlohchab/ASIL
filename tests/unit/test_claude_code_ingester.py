"""Phase 9.3 — unit tests for the Claude Code transcript ingester.

Drives the parser against a synthetic JSONL fixture so we never touch
the user's actual `~/.claude/projects/` directory."""

from __future__ import annotations

import json
from pathlib import Path

from asil_ingest_agents.claude_code import (
    ClaudeCodeIngester,
    _decode_project_dir,
    chunk_into_qa,
    find_claude_code_sessions,
    parse_session,
)


def _write_session(
    root: Path,
    *,
    project_dir_name: str,
    session_uuid: str,
    records: list[dict],
) -> Path:
    proj = root / project_dir_name
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / f"{session_uuid}.jsonl"
    with p.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return p


def test_decode_project_dir_inverts_the_slash_dash_encoding():
    assert (
        _decode_project_dir("-Users-alice-code-myrepo")
        == "/Users/alice/code/myrepo"
    )


def test_parse_session_filters_synthetic_tool_result_user_turns(tmp_path):
    """Claude Code injects synthetic user records whose content is just
    tool_result blocks. Those must NOT show up as user turns — only the
    real, typed user prompt does."""
    session = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-05-26T10:00:00Z",
            "message": {"role": "user", "content": "how does X work?"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-05-26T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "internal reasoning"},
                    {"type": "text", "text": "X works via Y."},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
                ],
            },
        },
        {
            # Synthetic — tool result fed back as 'user' role. Should be SKIPPED.
            "type": "user",
            "uuid": "u2-synthetic",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "stdout..."}],
            },
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "timestamp": "2026-05-26T10:00:10Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Confirmed."}]},
        },
        {"type": "file-history-snapshot", "snapshot": {}},
        {
            "type": "user",
            "uuid": "u3",
            "timestamp": "2026-05-26T10:00:30Z",
            "message": {"role": "user", "content": "another real question?"},
        },
    ]
    p = _write_session(
        tmp_path,
        project_dir_name="-fake-project",
        session_uuid="abc",
        records=session,
    )
    turns = parse_session(p)
    # 2 real user turns + 2 assistant turns; synthetic + file-history dropped.
    assert [t.role for t in turns] == ["user", "assistant", "assistant", "user"]
    assert turns[0].text == "how does X work?"
    assert turns[1].text == "X works via Y."  # thinking + tool_use stripped
    assert turns[2].text == "Confirmed."
    assert turns[3].text == "another real question?"


def test_chunk_into_qa_pairs_each_user_turn_with_following_assistant_text(tmp_path):
    """One QAChunk per real user turn; assistant turns until the next
    user turn get concatenated into the response."""
    session = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-05-26T10:00:00Z",
            "message": {"role": "user", "content": "q1?"},
        },
        {
            "type": "assistant",
            "uuid": "a1a",
            "timestamp": "2026-05-26T10:00:05Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "answer part 1"}]},
        },
        {
            "type": "assistant",
            "uuid": "a1b",
            "timestamp": "2026-05-26T10:00:08Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "answer part 2"}]},
        },
        {
            "type": "user",
            "uuid": "u2",
            "timestamp": "2026-05-26T10:00:30Z",
            "message": {"role": "user", "content": "q2?"},
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "timestamp": "2026-05-26T10:00:35Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "second answer"}]},
        },
    ]
    p = _write_session(tmp_path, project_dir_name="-p", session_uuid="s1", records=session)
    turns = parse_session(p)
    chunks = chunk_into_qa(turns, session_id="s1")
    assert len(chunks) == 2
    assert chunks[0].question == "q1?"
    assert "answer part 1" in chunks[0].assistant_response
    assert "answer part 2" in chunks[0].assistant_response
    assert chunks[1].question == "q2?"
    assert chunks[1].assistant_response == "second answer"
    assert chunks[0].source == "claude-code-transcript"
    assert chunks[0].turn_ids == ["u1", "a1a", "a1b"]


def test_chunk_into_qa_drops_orphan_assistant_turns(tmp_path):
    """Assistant turns before any user turn (rare but possible for
    automated sessions) should not produce a chunk."""
    session = [
        {"type": "assistant", "uuid": "a0", "message": {"role": "assistant", "content": [{"type": "text", "text": "preamble"}]}},
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "real question"}},
        {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]}},
    ]
    p = _write_session(tmp_path, project_dir_name="-p", session_uuid="s1", records=session)
    chunks = chunk_into_qa(parse_session(p), session_id="s1")
    assert len(chunks) == 1
    assert chunks[0].question == "real question"


def test_chunk_response_truncation():
    """Over-long assistant text gets tail-truncated so any single memory
    row stays at a reasonable size."""
    from asil_ingest_agents.base import Turn

    user = Turn(role="user", text="ping", message_id="u1")
    long_resp = Turn(role="assistant", text="x" * 5000, message_id="a1")
    chunks = chunk_into_qa([user, long_resp], session_id="s", max_response_chars=100)
    assert len(chunks) == 1
    assert chunks[0].assistant_response.startswith("x" * 100)
    assert chunks[0].assistant_response.endswith("[prose truncated]")


def test_find_claude_code_sessions_respects_project_filter(tmp_path):
    _write_session(tmp_path, project_dir_name="-a-project-one", session_uuid="s1", records=[])
    _write_session(tmp_path, project_dir_name="-a-project-two", session_uuid="s2", records=[])
    found = find_claude_code_sessions(root=tmp_path, project="one")
    assert [s.session_id for s in found] == ["s1"]


def test_ingester_plan_end_to_end(tmp_path):
    session = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-05-26T10:00:00Z",
            "message": {"role": "user", "content": "how does the cost ledger work?"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-05-26T10:00:10Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "It records every LLM call in asil_costs..."},
                ],
            },
        },
    ]
    _write_session(tmp_path, project_dir_name="-Users-me-proj", session_uuid="sess-1", records=session)
    plan = ClaudeCodeIngester(root=tmp_path).plan()
    assert plan.source == "claude-code-transcript"
    assert plan.sessions == ["sess-1"]
    assert len(plan.qa_chunks) == 1
    assert plan.qa_chunks[0].question == "how does the cost ledger work?"
    assert "asil_costs" in plan.qa_chunks[0].assistant_response


def test_ingester_skips_sessions_modified_before_since(tmp_path):
    """When `since` is set, sessions whose mtime is older are excluded."""
    import os
    import time
    from datetime import datetime, timedelta

    p = _write_session(tmp_path, project_dir_name="-old-proj", session_uuid="old", records=[])
    # Backdate the file 7 days.
    seven_days_ago = time.time() - 7 * 24 * 3600
    os.utime(p, (seven_days_ago, seven_days_ago))
    since = datetime.now() - timedelta(hours=1)
    plan = ClaudeCodeIngester(root=tmp_path).plan(since=since)
    assert plan.sessions == []
