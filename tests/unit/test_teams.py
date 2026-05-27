"""Phase 9.5 — unit tests for TeamsStore + the API auth middleware.

We mock psycopg.connect so the tests don't need a live Postgres."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import bcrypt
import pytest
from asil_memory.teams import TeamsStore


def _mock_cursor(*, fetchone_returns=None, rowcount: int = 1):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = fetchone_returns
    cur.fetchall.return_value = []
    cur.rowcount = rowcount
    return cur


def _mock_conn(cur):
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur
    return conn


def test_create_team_returns_api_key_with_team_id_prefix():
    cur = _mock_cursor(
        fetchone_returns={
            "id": "startup-dev",
            "name": "Startup Dev",
            "created_at": datetime(2026, 5, 27, tzinfo=UTC),
            "revoked_at": None,
        }
    )
    conn = _mock_conn(cur)
    store = TeamsStore("postgresql://x")
    with patch("asil_memory.teams.psycopg.connect", return_value=conn):
        out = store.create_team(team_id="startup-dev", name="Startup Dev")
    # Key format is `asil_<team_id>_<secret>` so verify_key can O(1) lookup.
    assert out.api_key.startswith("asil_startup-dev_")
    assert out.team.id == "startup-dev"
    # The cursor was asked to insert a bcrypt hash, NOT the raw key.
    insert_args = cur.execute.call_args[0][1]
    assert insert_args[0] == "startup-dev"
    assert insert_args[2] != out.api_key
    assert insert_args[2].startswith("$2b$")  # bcrypt hash sigil


def test_create_team_rejects_non_alphanumeric_team_id():
    store = TeamsStore("postgresql://x")
    with pytest.raises(ValueError, match="alphanumeric"):
        store.create_team(team_id="has spaces", name="x")
    with pytest.raises(ValueError, match="alphanumeric"):
        store.create_team(team_id="", name="x")


def test_verify_key_returns_team_id_on_valid_active_key():
    """Round-trip: create a key, then verify_key returns the same team_id."""
    api_key = "asil_team-x_" + "secret-fixture-1234"
    stored = bcrypt.hashpw(api_key.encode(), bcrypt.gensalt()).decode()
    cur = _mock_cursor(fetchone_returns={"api_key_hash": stored, "revoked_at": None})
    conn = _mock_conn(cur)
    store = TeamsStore("postgresql://x")
    with patch("asil_memory.teams.psycopg.connect", return_value=conn):
        assert store.verify_key(api_key) == "team-x"


def test_verify_key_returns_none_on_revoked_team():
    api_key = "asil_team-x_secret"
    stored = bcrypt.hashpw(api_key.encode(), bcrypt.gensalt()).decode()
    cur = _mock_cursor(
        fetchone_returns={
            "api_key_hash": stored,
            "revoked_at": datetime(2026, 5, 27, tzinfo=UTC),
        }
    )
    conn = _mock_conn(cur)
    store = TeamsStore("postgresql://x")
    with patch("asil_memory.teams.psycopg.connect", return_value=conn):
        assert store.verify_key(api_key) is None


def test_verify_key_returns_none_on_unknown_team():
    cur = _mock_cursor(fetchone_returns=None)
    conn = _mock_conn(cur)
    store = TeamsStore("postgresql://x")
    with patch("asil_memory.teams.psycopg.connect", return_value=conn):
        assert store.verify_key("asil_nonexistent_blah") is None


def test_verify_key_rejects_malformed_keys():
    store = TeamsStore("postgresql://x")
    assert store.verify_key("") is None
    assert store.verify_key("not-an-asil-key") is None
    assert store.verify_key("asil_no-underscore-after-prefix") is None


def test_verify_key_rejects_wrong_secret_with_matching_team_id():
    """An attacker knowing the team_id can't bypass bcrypt — the secret
    portion is still verified against the stored hash."""
    real_key = "asil_team-x_correct-secret"
    stored = bcrypt.hashpw(real_key.encode(), bcrypt.gensalt()).decode()
    cur = _mock_cursor(fetchone_returns={"api_key_hash": stored, "revoked_at": None})
    conn = _mock_conn(cur)
    store = TeamsStore("postgresql://x")
    with patch("asil_memory.teams.psycopg.connect", return_value=conn):
        assert store.verify_key("asil_team-x_WRONG-secret") is None


def test_rotate_key_mints_new_secret_for_existing_team():
    cur = _mock_cursor(
        fetchone_returns={
            "id": "team-x",
            "name": "Team X",
            "created_at": datetime(2026, 5, 27, tzinfo=UTC),
            "revoked_at": None,
        }
    )
    conn = _mock_conn(cur)
    store = TeamsStore("postgresql://x")
    with patch("asil_memory.teams.psycopg.connect", return_value=conn):
        out1 = store.rotate_key(team_id="team-x")
        # cursor returns the same row each time we call it, but each
        # rotate_key call mints a fresh random secret.
        out2 = store.rotate_key(team_id="team-x")
    assert out1.api_key != out2.api_key
    assert out1.api_key.startswith("asil_team-x_")


def test_rotate_key_raises_on_unknown_team():
    cur = _mock_cursor(fetchone_returns=None)
    conn = _mock_conn(cur)
    store = TeamsStore("postgresql://x")
    with (
        patch("asil_memory.teams.psycopg.connect", return_value=conn),
        pytest.raises(KeyError, match="does not exist"),
    ):
        store.rotate_key(team_id="ghost")


def test_revoke_returns_true_when_row_affected():
    cur = _mock_cursor(rowcount=1)
    conn = _mock_conn(cur)
    store = TeamsStore("postgresql://x")
    with patch("asil_memory.teams.psycopg.connect", return_value=conn):
        assert store.revoke(team_id="team-x") is True


def test_revoke_returns_false_when_already_revoked_or_missing():
    cur = _mock_cursor(rowcount=0)
    conn = _mock_conn(cur)
    store = TeamsStore("postgresql://x")
    with patch("asil_memory.teams.psycopg.connect", return_value=conn):
        assert store.revoke(team_id="team-x") is False


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


def test_auth_middleware_allows_public_paths_without_key(monkeypatch):
    """`/health`, `/llm/profile`, `/llm/ping`, `/mcp/tools` must work
    without an Authorization header even when auth is enabled."""
    monkeypatch.delenv("ASIL_AUTH_DISABLE", raising=False)
    from asil_api.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        # /mcp/tools is the catalog — public so agents can discover the surface.
        r = c.get("/mcp/tools")
        assert r.status_code == 200


def test_auth_middleware_blocks_protected_path_without_key(monkeypatch):
    monkeypatch.delenv("ASIL_AUTH_DISABLE", raising=False)
    from asil_api.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.post("/mcp/call/asil.who_owns", json={"arguments": {"file_path": "x"}})
        assert r.status_code == 401
        assert "Authorization" in r.json()["error"]


def test_auth_middleware_disable_env_bypasses_check(monkeypatch):
    """`ASIL_AUTH_DISABLE=true` is the local-dev escape hatch — no key
    needed, every request gets team_id='default'."""
    monkeypatch.setenv("ASIL_AUTH_DISABLE", "true")
    from asil_api.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        # Protected path but no Auth header → still allowed.
        # (The endpoint itself may fail downstream because there's no live
        # Neo4j, but it should NOT return 401.)
        r = c.post("/mcp/call/asil.who_owns", json={"arguments": {"file_path": "x"}})
        assert r.status_code != 401
