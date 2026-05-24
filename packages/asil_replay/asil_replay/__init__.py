"""ASIL execution replay engine — the hero demo.

Given an incident ID, reconstructs the full story:
  - Chronological timeline of all runtime events across affected services
  - Top causes (from :PRECEDED edges written by the temporal linker)
  - Service cascade (services ordered by their earliest event time)
  - Before/after state diff (deployments and metric changes during the window)
  - Aggregated confidence card

The replay engine does NOT invent causes — it reads :PRECEDED edges that
the Phase 4 linker wrote. If there are no causal edges, the replay still
produces a timeline and cascade, but the causes list is empty.
"""

from asil_replay.replay import IncidentReplay, ReplayEngine
from asil_replay.state_diff import StateDiff, StateDiffer

__version__ = "0.0.1"

__all__ = [
    "IncidentReplay",
    "ReplayEngine",
    "StateDiff",
    "StateDiffer",
]
