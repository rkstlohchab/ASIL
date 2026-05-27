"""Tests for the Phase-9-followup work:

* `_summarise_actions` / `_summarise_final_todos` turn raw tool_use
  blocks into the human-readable 'Actions taken' + 'Final task list'
  sections that get stored in `assistant_response`.
* `EpisodicStore.forget_session` / `clear_all` for cleanup.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from asil_ingest_agents.claude_code import (
    _summarise_actions,
    _summarise_final_todos,
    chunk_into_qa,
    parse_session,
)
from asil_memory.episodic import EpisodicStore

# ---------------------------------------------------------------------------
# tool capture
# ---------------------------------------------------------------------------


def test_summarise_actions_buckets_file_edits_and_writes():
    tool_uses = [
        {"name": "Read", "input": {"file_path": "/Users/me/repo/foo.py"}},
        {"name": "Read", "input": {"file_path": "/Users/me/repo/bar.py"}},
        {"name": "Edit", "input": {"file_path": "/Users/me/Documents/GitHub/proj/x.py", "old_string": "a", "new_string": "b"}},
        {"name": "Write", "input": {"file_path": "/Users/me/Documents/GitHub/proj/y.py", "content": "..."}},
        {"name": "Bash", "input": {"command": "make test", "description": "Run unit tests"}},
    ]
    out = _summarise_actions(tool_uses)
    assert "**Actions taken in this turn:**" in out
    assert "**Edited:**" in out
    assert "x.py" in out
    assert "**Wrote:**" in out
    assert "y.py" in out
    assert "**Read:**" in out
    assert "**Ran:**" in out
    assert "`make test`" in out
    assert "Run unit tests" in out


def test_summarise_actions_returns_empty_for_no_tools():
    assert _summarise_actions([]) == ""


def test_summarise_actions_dedupes_repeated_files():
    """Reading the same file 3 times in a turn should list it once."""
    tool_uses = [
        {"name": "Read", "input": {"file_path": "/repo/a.py"}},
        {"name": "Read", "input": {"file_path": "/repo/a.py"}},
        {"name": "Read", "input": {"file_path": "/repo/a.py"}},
    ]
    out = _summarise_actions(tool_uses)
    assert out.count("a.py") == 1


def test_summarise_actions_truncates_long_read_lists():
    tool_uses = [
        {"name": "Read", "input": {"file_path": f"/r/file{i}.py"}} for i in range(25)
    ]
    out = _summarise_actions(tool_uses)
    assert "+15 more" in out  # 25 total, show 10, +15 more


def test_summarise_actions_captures_subagent_calls():
    tool_uses = [
        {
            "name": "Agent",
            "input": {
                "description": "Audit branch ship-readiness",
                "subagent_type": "general-purpose",
            },
        }
    ]
    out = _summarise_actions(tool_uses)
    assert "**Sub-agents:**" in out
    assert "general-purpose" in out
    assert "Audit branch ship-readiness" in out


def test_summarise_final_todos_emits_status_icons():
    tool_uses = [
        {
            "name": "TodoWrite",
            "input": {
                "todos": [
                    {"content": "Task A", "status": "completed", "activeForm": "Doing A"},
                    {"content": "Task B", "status": "in_progress", "activeForm": "Doing B"},
                    {"content": "Task C", "status": "pending", "activeForm": "Doing C"},
                ]
            },
        }
    ]
    out = _summarise_final_todos(tool_uses)
    assert "**Final task list:**" in out
    assert "✅ Task A" in out
    assert "⏳ Task B" in out
    assert "⬜ Task C" in out


def test_summarise_final_todos_takes_last_call_only():
    """Multiple TodoWrite calls in one turn → only the last one (current
    state) is rendered. Earlier states are noise."""
    tool_uses = [
        {"name": "TodoWrite", "input": {"todos": [{"content": "old", "status": "pending", "activeForm": ""}]}},
        {"name": "TodoWrite", "input": {"todos": [{"content": "current", "status": "in_progress", "activeForm": ""}]}},
    ]
    out = _summarise_final_todos(tool_uses)
    assert "current" in out
    assert "old" not in out


def test_parse_session_captures_tool_uses_in_turn_extra(tmp_path):
    """End-to-end: a JSONL with tool_use blocks results in Turn objects
    carrying those tool_uses in extra so the chunker can use them."""
    p = tmp_path / "sess.jsonl"
    with p.open("w") as f:
        f.write(json.dumps({
            "type": "user",
            "uuid": "u1",
            "message": {"role": "user", "content": "fix the auth bug"},
        }) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "uuid": "a1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Looking at auth.py..."},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/auth.py"}},
                    {"type": "tool_use", "name": "Edit", "input": {
                        "file_path": "/repo/auth.py", "old_string": "x", "new_string": "y"
                    }},
                ],
            },
        }) + "\n")
    turns = parse_session(p)
    assistant_turn = turns[1]
    assert assistant_turn.extra["tool_uses"][0]["name"] == "Read"
    assert assistant_turn.extra["tool_uses"][1]["name"] == "Edit"


def test_chunk_assistant_response_includes_actions_and_todos(tmp_path):
    """The whole pipeline: parse → chunk → assistant_response carries
    prose + Actions + Final task list, all in one block."""
    p = tmp_path / "sess.jsonl"
    with p.open("w") as f:
        f.write(json.dumps({
            "type": "user",
            "uuid": "u1",
            "message": {"role": "user", "content": "implement feature X"},
        }) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "uuid": "a1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll add feature X."},
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "/r/x.py", "content": "..."}},
                    {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [
                        {"content": "Add feature X", "status": "completed", "activeForm": ""},
                    ]}},
                ],
            },
        }) + "\n")
    chunks = chunk_into_qa(parse_session(p), session_id="s1")
    assert len(chunks) == 1
    resp = chunks[0].assistant_response
    assert "I'll add feature X." in resp
    assert "**Actions taken in this turn:**" in resp
    assert "x.py" in resp
    assert "**Final task list:**" in resp
    assert "✅ Add feature X" in resp


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------


def _store_with_mocked_conn():
    store = EpisodicStore.__new__(EpisodicStore)
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur
    store._conn = conn
    store._vector = None
    return store, cur


def test_forget_session_returns_zero_when_nothing_matches():
    store, cur = _store_with_mocked_conn()
    cur.fetchall.return_value = []
    cur.rowcount = 0
    n = store.forget_session("nonexistent-session")
    assert n == 0


def test_forget_session_deletes_matching_rows():
    """Match on both `origin_session_id` and `metadata.original_session_id`
    so both 'memory-written-during-session' and 'memory-ingested-from-session'
    cases are covered."""
    store, cur = _store_with_mocked_conn()
    cur.fetchall.return_value = [("00000000-0000-0000-0000-000000000001",), ("00000000-0000-0000-0000-000000000002",)]
    cur.rowcount = 2
    n = store.forget_session("session-abc")
    assert n == 2

    # First call selected matching IDs.
    select_sql = cur.execute.call_args_list[0][0][0]
    assert "origin_session_id" in select_sql
    assert "original_session_id" in select_sql
    assert cur.execute.call_args_list[0][0][1] == ("session-abc", "session-abc")

    # Second call deleted by id list.
    delete_sql = cur.execute.call_args_list[1][0][0]
    assert "DELETE FROM asil_memories" in delete_sql
    assert "= ANY(%s::uuid[])" in delete_sql


def test_clear_all_deletes_everything():
    store, cur = _store_with_mocked_conn()
    cur.rowcount = 42
    n = store.clear_all()
    assert n == 42
    sql = cur.execute.call_args[0][0]
    assert "DELETE FROM asil_memories" in sql
    # No WHERE clause — everything goes.
    assert "WHERE" not in sql.upper()
