"""Identity helpers — resolve the user, machine, and (sometimes) agent that
originated a memory write so the cross-team / cross-agent provenance preamble
has something concrete to render.

Resolution rules (every lookup is cheap and cached on the module):

* `get_user_id()`
    1. `ASIL_USER_ID` env var if set.
    2. `git config user.email` from the current working directory (best-effort).
    3. `"unknown"`.

* `get_machine_id()`
    1. `ASIL_MACHINE_ID` env var if set.
    2. A stable per-machine hash persisted at `~/.asil/machine_id` on first
       use. Generated from `socket.gethostname()` plus a random salt.
    3. The bare hostname if writing the cache file fails.

* `get_origin_agent(explicit: str | None)`
    Trivial passthrough — returns `explicit` if set, else `"cli"`. The CLI
    bakes in `"cli"`; MCP handlers pass through the `client_id` arg.

These functions never raise; they always return *some* string. The downstream
schema stores them as `TEXT NOT NULL DEFAULT 'unknown'` so partial resolution
never blocks a write.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import socket
import subprocess
from functools import lru_cache
from pathlib import Path

_MACHINE_ID_PATH = Path.home() / ".asil" / "machine_id"


@lru_cache(maxsize=1)
def get_user_id() -> str:
    env = os.environ.get("ASIL_USER_ID")
    if env:
        return env.strip()
    try:
        out = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        email = out.stdout.strip()
        if email:
            return email
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown"


@lru_cache(maxsize=1)
def get_machine_id() -> str:
    env = os.environ.get("ASIL_MACHINE_ID")
    if env:
        return env.strip()
    try:
        if _MACHINE_ID_PATH.exists():
            value = _MACHINE_ID_PATH.read_text().strip()
            if value:
                return value
        _MACHINE_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        salt = secrets.token_hex(8)
        host = socket.gethostname() or "unknown-host"
        digest = hashlib.sha256(f"{host}:{salt}".encode()).hexdigest()[:16]
        _MACHINE_ID_PATH.write_text(digest)
        return digest
    except OSError:
        try:
            return socket.gethostname() or "unknown"
        except OSError:
            return "unknown"


def get_origin_agent(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    return "cli"


def reset_caches_for_tests() -> None:
    """Pytest hook — let tests with `monkeypatch.setenv` see the new value."""
    get_user_id.cache_clear()
    get_machine_id.cache_clear()
