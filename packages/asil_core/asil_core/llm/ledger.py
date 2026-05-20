"""Cost ledger — every LLM call records cost so we can enforce daily budgets.

Phase 0 ships only the in-memory implementation. A Postgres-backed ledger
will replace it in Phase 2 (memory layer). Both satisfy the `CostLedger`
protocol so the router doesn't change.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol


@dataclass(slots=True)
class CostRecord:
    timestamp: datetime
    provider: str
    model: str
    tier: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    profile: str


class CostLedger(Protocol):
    async def record(self, entry: CostRecord) -> None: ...
    async def spend_today_usd(self) -> float: ...


class InMemoryCostLedger:
    """Process-local ledger. Loses state on restart — fine for dev / tests."""

    def __init__(self) -> None:
        self._by_day: dict[date, float] = defaultdict(float)
        self._records: list[CostRecord] = []

    async def record(self, entry: CostRecord) -> None:
        self._by_day[entry.timestamp.astimezone(UTC).date()] += entry.cost_usd
        self._records.append(entry)

    async def spend_today_usd(self) -> float:
        today = datetime.now(UTC).date()
        return self._by_day[today]

    def all_records(self) -> list[CostRecord]:
        return list(self._records)
