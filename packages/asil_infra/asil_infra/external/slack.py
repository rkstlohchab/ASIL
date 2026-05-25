"""Slack channel adapter — token-gated.

Reads recent messages from one or more channels and emits a `ChatMessage`
per message. Extracts incident IDs and service names from the body so the
graph builder can wire `(:ChatMessage)-[:DISCUSSES]->(:Incident)` and
`(:ChatMessage)-[:MENTIONS]->(:Service)` edges.

Token: set `SLACK_BOT_TOKEN` (a `xoxb-...` token from a Slack app with
`channels:history` + `groups:history` scopes). Without it, `poll()`
raises `NotConfiguredError` — the rest of the system keeps working.

This adapter is intentionally read-only. It does not post, react, or
mutate Slack state.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from asil_infra.adapters import NotConfiguredError
from asil_infra.external_models import ChatMessage

# Match `INC-2026-04-12-...`, `INC1234`, `INCIDENT-42` and similar incident
# id shapes. The grouped capture is the full id; we leave normalisation to
# downstream code.
_INCIDENT_ID_RE = re.compile(r"\b(INC(?:IDENT)?-?\d[A-Za-z0-9-]*)\b", re.IGNORECASE)


class SlackAdapter:
    def __init__(
        self,
        channels: list[str],
        *,
        token: str | None = None,
        lookback_seconds: int = 86_400,
        limit: int = 200,
        known_services: list[str] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._channels = channels
        self._token = token or os.environ.get("SLACK_BOT_TOKEN")
        self._lookback = lookback_seconds
        self._limit = limit
        self._known_services = known_services or []
        self._timeout = timeout_seconds

    async def poll(self, env_key: str | None = None) -> list[ChatMessage]:
        if not self._token:
            raise NotConfiguredError(
                "SLACK_BOT_TOKEN not set. Create a Slack app with "
                "channels:history scope and export the bot token."
            )
        if not self._channels:
            return []

        try:
            import httpx
        except ImportError as exc:
            raise NotConfiguredError("httpx not installed") from exc

        oldest = (datetime.now(UTC) - timedelta(seconds=self._lookback)).timestamp()
        out: list[ChatMessage] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for channel in self._channels:
                r = await client.get(
                    "https://slack.com/api/conversations.history",
                    headers={"Authorization": f"Bearer {self._token}"},
                    params={
                        "channel": channel,
                        "oldest": str(oldest),
                        "limit": str(self._limit),
                    },
                )
                body = r.json() if r.status_code == 200 else {}
                if not body.get("ok"):
                    # surface auth/permission errors as not-configured, not crash
                    raise NotConfiguredError(
                        f"slack {channel}: {body.get('error', 'unknown error')}"
                    )
                for msg in body.get("messages", []):
                    parsed = self._parse_message(channel, msg)
                    if parsed is not None:
                        out.append(parsed)
        return out

    def _parse_message(self, channel: str, msg: dict[str, Any]) -> ChatMessage | None:
        text = msg.get("text", "")
        ts = msg.get("ts")
        if not text or not ts:
            return None
        posted_at = datetime.fromtimestamp(float(ts), tz=UTC)
        incident_ids = self._extract_incident_ids(text)
        services = self._extract_services(text)
        return ChatMessage(
            source=f"slack://{channel}",
            external_id=ts,
            channel=channel,
            ts=ts,
            author=msg.get("user") or msg.get("bot_id"),
            text=text,
            posted_at=posted_at,
            permalink=None,  # would need a second permalink call per message
            incident_ids=incident_ids,
            service_names=services,
        )

    @staticmethod
    def _extract_incident_ids(text: str) -> list[str]:
        return list({m.group(1) for m in _INCIDENT_ID_RE.finditer(text)})

    def _extract_services(self, text: str) -> list[str]:
        if not self._known_services:
            return []
        lowered = text.lower()
        return [svc for svc in self._known_services if svc.lower() in lowered]
