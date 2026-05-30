"""Multi-team auth — Phase 9.5.

A `Team` is the unit of memory-sharing. Multiple machines configured
with the same API key get the same `team_id`, and memories written by
any of them are recallable by all of them. Different keys → different
`team_id` → isolated pools.

The key is shown to the user exactly once on creation; only its bcrypt
hash is stored. Lost keys can be rotated via `rotate_key` (which mints
a new one and invalidates the old) or revoked via `revoke`.

`ASIL_AUTH_DISABLE=true` tells the API middleware to skip every check
and stamp every request with `team_id='default'`. That keeps the
one-command local dev experience working while real deployments lock
things down."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import bcrypt
import psycopg
from psycopg.rows import dict_row

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS asil_teams (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    api_key_hash  TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ
);
"""

# Format: asil_<team_id>_<24-char-secret>. The team_id prefix means key
# lookup is O(1) — we don't have to bcrypt-verify against every team.
_KEY_PREFIX = "asil_"


@dataclass(slots=True)
class Team:
    id: str
    name: str
    created_at: datetime
    revoked_at: datetime | None


@dataclass(slots=True)
class TeamWithKey:
    """Returned only by `create_team` / `rotate_key`. The raw `api_key`
    is shown to the user once; after that, only the bcrypt hash is
    persisted."""

    team: Team
    api_key: str


class TeamsStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = _normalize_dsn(dsn)

    def _conn(self):
        return psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)

    def apply_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)

    # --- writes

    def create_team(self, *, team_id: str, name: str) -> TeamWithKey:
        """Mint a new team + a fresh API key. Returns the key in cleartext
        exactly once — the caller is responsible for showing it to the
        user and then discarding it."""
        if not team_id or not team_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("team_id must be alphanumeric (with - or _ allowed)")
        raw_secret = secrets.token_urlsafe(24)
        api_key = f"{_KEY_PREFIX}{team_id}_{raw_secret}"
        key_hash = bcrypt.hashpw(api_key.encode(), bcrypt.gensalt()).decode()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO asil_teams (id, name, api_key_hash) VALUES (%s, %s, %s) "
                "RETURNING id, name, created_at, revoked_at",
                (team_id, name, key_hash),
            )
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT returned no row")
        return TeamWithKey(team=_row_to_team(row), api_key=api_key)

    def rotate_key(self, *, team_id: str) -> TeamWithKey:
        """Mint a new key for an existing team; old key stops working
        immediately. Returns the new key in cleartext exactly once."""
        raw_secret = secrets.token_urlsafe(24)
        api_key = f"{_KEY_PREFIX}{team_id}_{raw_secret}"
        key_hash = bcrypt.hashpw(api_key.encode(), bcrypt.gensalt()).decode()
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE asil_teams SET api_key_hash = %s WHERE id = %s "
                "RETURNING id, name, created_at, revoked_at",
                (key_hash, team_id),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"team {team_id!r} does not exist")
        return TeamWithKey(team=_row_to_team(row), api_key=api_key)

    def revoke(self, *, team_id: str) -> bool:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE asil_teams SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
                (team_id,),
            )
            return cur.rowcount > 0

    # --- reads

    def list_teams(self) -> list[Team]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, created_at, revoked_at FROM asil_teams ORDER BY created_at"
            )
            return [_row_to_team(r) for r in cur.fetchall()]

    def verify_key(self, api_key: str) -> str | None:
        """Return the matched `team_id` if the key is valid + non-revoked,
        else None. O(1) lookup because the team_id is embedded in the
        key prefix — we don't have to bcrypt-verify against every team."""
        if not api_key or not api_key.startswith(_KEY_PREFIX):
            return None
        # Strip prefix, split on first underscore: <team_id>_<secret>.
        rest = api_key[len(_KEY_PREFIX) :]
        if "_" not in rest:
            return None
        team_id, _ = rest.split("_", 1)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT api_key_hash, revoked_at FROM asil_teams WHERE id = %s",
                (team_id,),
            )
            row = cur.fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        stored_hash = row["api_key_hash"]
        try:
            if bcrypt.checkpw(api_key.encode(), stored_hash.encode()):
                return team_id
        except (ValueError, TypeError):
            pass
        return None


def _row_to_team(row: dict[str, Any]) -> Team:
    return Team(
        id=row["id"],
        name=row["name"],
        created_at=row["created_at"],
        revoked_at=row.get("revoked_at"),
    )


def _normalize_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+"):
        prefix, _, rest = dsn.partition("://")
        scheme = prefix.split("+", 1)[0]
        return f"{scheme}://{rest}"
    return dsn
