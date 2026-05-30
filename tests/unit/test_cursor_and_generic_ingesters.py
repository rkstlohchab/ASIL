"""Phase 9.4 — fixture-based tests for Cursor + generic JSONL ingesters
and the polling watch loop."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from asil_ingest_agents.cursor import CursorIngester, _extract_turns, _read_chat_blobs
from asil_ingest_agents.generic_jsonl import GenericJsonlIngester
from asil_ingest_agents.watch import WatchTick, run_watch_loop

# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def _write_cursor_db(root: Path, *, workspace_id: str, blob: dict) -> Path:
    ws = root / workspace_id
    ws.mkdir(parents=True, exist_ok=True)
    db = ws / "state.vscdb"
    conn = sqlite3.connect(db)
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        cur.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            ("workbench.panel.aichat.view.aichat.chatdata", json.dumps(blob)),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_cursor_extract_turns_handles_tabs_with_bubbles():
    blob = {
        "tabs": [
            {
                "id": "tab-1",
                "bubbles": [
                    {"type": "user", "text": "how does auth work?"},
                    {"type": "ai", "text": "It uses JWT in the middleware."},
                    {"type": "user", "text": "and refresh tokens?"},
                    {"type": "ai", "text": "Stored in Redis with TTL."},
                ],
            }
        ]
    }
    turns = _extract_turns(blob)
    assert [t.role for t in turns] == ["user", "assistant", "user", "assistant"]
    assert turns[0].text == "how does auth work?"
    assert turns[3].text == "Stored in Redis with TTL."


def test_cursor_extract_turns_handles_messages_with_role_field():
    blob = {
        "id": "session-99",
        "messages": [
            {"role": "user", "content": "what is X?"},
            {"role": "assistant", "content": "X is the thing."},
        ],
    }
    turns = _extract_turns(blob)
    assert len(turns) == 2
    assert turns[0].role == "user"


def test_cursor_extract_turns_handles_content_as_block_list():
    """Cursor sometimes stores assistant text as `[{type:'text', text:'…'}]`."""
    blob = {
        "messages": [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "answer part 1"},
                    {"type": "text", "text": "answer part 2"},
                ],
            },
        ]
    }
    turns = _extract_turns(blob)
    assert turns[1].text == "answer part 1\nanswer part 2"


def test_cursor_ingester_end_to_end(tmp_path):
    _write_cursor_db(
        tmp_path,
        workspace_id="ws-abc-1234",
        blob={
            "tabs": [
                {
                    "id": "tab-1",
                    "bubbles": [
                        {"type": "user", "text": "ingester test"},
                        {"type": "ai", "text": "ok"},
                    ],
                }
            ]
        },
    )
    plan = CursorIngester(root=tmp_path).plan()
    assert plan.source == "cursor-transcript"
    assert plan.sessions == ["ws-abc-1234"]
    assert len(plan.qa_chunks) == 1
    assert plan.qa_chunks[0].question == "ingester test"
    assert plan.qa_chunks[0].assistant_response == "ok"


def test_cursor_read_blobs_handles_missing_table(tmp_path):
    """If `state.vscdb` has no ItemTable, return [] rather than raise."""
    db = tmp_path / "empty.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE Other (x INTEGER)")
    conn.close()
    assert _read_chat_blobs(db) == []


# ---------------------------------------------------------------------------
# Generic JSONL
# ---------------------------------------------------------------------------


def test_generic_jsonl_default_role_text_keys(tmp_path):
    p = tmp_path / "aider.jsonl"
    with p.open("w") as f:
        f.write(json.dumps({"role": "user", "content": "q1?"}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "a1"}) + "\n")
        f.write(json.dumps({"role": "user", "content": "q2?"}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "a2"}) + "\n")
    plan = GenericJsonlIngester(paths=[p], source="aider-transcript").plan()
    assert plan.source == "aider-transcript"
    assert len(plan.qa_chunks) == 2
    assert plan.qa_chunks[0].question == "q1?"
    assert plan.qa_chunks[1].assistant_response == "a2"


def test_generic_jsonl_custom_field_mapping(tmp_path):
    """Different agents use different field names — `--role-key` /
    `--text-key` / `--user-label` make the parser configurable."""
    p = tmp_path / "weird.jsonl"
    with p.open("w") as f:
        f.write(json.dumps({"who": "human", "msg": "ping"}) + "\n")
        f.write(json.dumps({"who": "bot", "msg": "pong"}) + "\n")
    plan = GenericJsonlIngester(
        paths=[p],
        role_key="who",
        text_key="msg",
        user_label="human",
        assistant_label="bot",
        ts_key=None,
    ).plan()
    assert len(plan.qa_chunks) == 1
    assert plan.qa_chunks[0].question == "ping"
    assert plan.qa_chunks[0].assistant_response == "pong"


def test_generic_jsonl_skips_unrecognised_roles(tmp_path):
    p = tmp_path / "noisy.jsonl"
    with p.open("w") as f:
        f.write(json.dumps({"role": "system", "content": "boot"}) + "\n")
        f.write(json.dumps({"role": "user", "content": "real"}) + "\n")
        f.write(json.dumps({"role": "tool", "content": "ignore me"}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "ok"}) + "\n")
    plan = GenericJsonlIngester(paths=[p]).plan()
    assert len(plan.qa_chunks) == 1
    assert plan.qa_chunks[0].question == "real"
    assert plan.qa_chunks[0].assistant_response == "ok"


# ---------------------------------------------------------------------------
# Watch daemon
# ---------------------------------------------------------------------------


def test_watch_loop_runs_n_iterations_then_exits():
    calls: list[WatchTick] = []

    def on_tick(t: WatchTick) -> None:
        calls.append(t)

    # No-op sleep so the test runs instantly.
    run_watch_loop(
        interval_seconds=1,
        overlap_seconds=2,
        on_tick=on_tick,
        max_iterations=3,
        sleep=lambda _s: None,
    )
    assert len(calls) == 3
    assert calls[0].iteration == 0
    assert calls[2].iteration == 2


def test_watch_loop_swallows_per_tick_errors():
    """A single bad tick must not kill the daemon — log and move on."""
    calls = []

    def on_tick(t: WatchTick) -> None:
        calls.append(t.iteration)
        if t.iteration == 1:
            raise RuntimeError("transient")

    run_watch_loop(
        interval_seconds=0,
        on_tick=on_tick,
        max_iterations=3,
        sleep=lambda _s: None,
    )
    # Iteration 1 raised, but iterations 2+ still ran.
    assert calls == [0, 1, 2]


def test_watch_loop_since_window_includes_overlap():
    """`since` should be `now - interval - overlap` so brief stalls don't
    drop chunks. Verify the math (within a generous tolerance)."""
    captured: list[WatchTick] = []

    def on_tick(t: WatchTick) -> None:
        captured.append(t)

    run_watch_loop(
        interval_seconds=30,
        overlap_seconds=60,
        on_tick=on_tick,
        max_iterations=1,
        sleep=lambda _s: None,
    )
    t = captured[0]
    delta = (t.started_at - t.since).total_seconds()
    assert 85 < delta < 95  # 30 + 60 = 90s window


def test_generic_jsonl_respects_since(tmp_path):
    """When --since is set, files older than the cutoff are skipped."""
    import os
    import time

    p = tmp_path / "old.jsonl"
    p.write_text(json.dumps({"role": "user", "content": "old"}) + "\n")
    old = time.time() - 7 * 24 * 3600
    os.utime(p, (old, old))
    plan = GenericJsonlIngester(paths=[p]).plan(since=datetime.now() - timedelta(hours=1))
    assert plan.qa_chunks == []
