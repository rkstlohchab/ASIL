"""`asil scan` — the SonarQube-style CI entry point.

One command that runs every check ASIL has on a repo + emits the
results in a format CI can consume:

  - drift events against a stored baseline (Phase 6).
  - causal links between recent incidents and files changed in the
    current branch (Phase 4 surfaced as risk signal).
  - low-confidence Q&A coverage on an optional `qa_corpus` (Phase 2's
    eval harness, run as a PR gate instead of a regression test).

The orchestrator collects everything into a `ScanReport`. The CLI then
fans the report out to whatever output format CI asked for: terminal,
JSON, SARIF, or a markdown PR comment.

Quality gate is just "max severity in the report > threshold -> exit
non-zero." Strict / normal / lenient / none, like every other linter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class Severity(StrEnum):
    """Mirrors SARIF's `level` field (error / warning / note) plus a
    distinguished `critical` so the gate can be strict about a real
    sub-set of `error`-level findings."""

    critical = "critical"
    error = "error"
    warning = "warning"
    note = "note"


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.note: 0,
    Severity.warning: 1,
    Severity.error: 2,
    Severity.critical: 3,
}


@dataclass(slots=True)
class ScanFinding:
    """One thing ASIL noticed.

    Fields are deliberately SARIF-shaped so `to_sarif` is a 1:1 mapping
    rather than a translation: every field below has a direct slot in
    `result.locations` / `result.message` / `result.ruleId` / `result.level`.
    """

    rule_id: str  # "drift/boundary-violation", "incident/recent-cause", ...
    severity: Severity
    message: str  # human-readable, ASCII only — PR comment safe
    file_path: str | None = None
    line: int | None = None
    derivation: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScanReport:
    """Top-level scan output."""

    repo_root: str
    repo_key: str
    started_at: datetime
    duration_seconds: float
    findings: list[ScanFinding]
    gate: str  # "strict" | "normal" | "lenient" | "none"
    passed_gate: bool

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {s.value: 0 for s in Severity}
        for f in self.findings:
            out[f.severity.value] += 1
        return out

    @property
    def max_severity(self) -> Severity | None:
        if not self.findings:
            return None
        return max(self.findings, key=lambda f: _SEVERITY_RANK[f.severity]).severity


# --------------------------------------------------------------------- runner


def run_scan(
    *,
    repo_root: str | Path,
    repo_key: str,
    baseline_path: str | Path | None,
    gate: str = "normal",
    include_recent_incidents: bool = True,
    incident_lookback_hours: int = 168,
) -> ScanReport:
    """Synchronously collect findings from every Phase-6 / Phase-4 source
    we have, plus optionally the Q&A corpus when one is configured.

    Avoids touching the LLM router on the scan path — every signal here
    comes from observable graph state or a saved baseline. That keeps
    `asil scan` cheap enough to run on every PR.
    """
    import time

    started = datetime.now(UTC)
    t0 = time.monotonic()

    repo_root = Path(repo_root).resolve()
    findings: list[ScanFinding] = []

    findings.extend(_collect_drift(repo_key, baseline_path))
    if include_recent_incidents:
        findings.extend(
            _collect_recent_incident_causes(repo_key, lookback_hours=incident_lookback_hours)
        )

    duration = round(time.monotonic() - t0, 3)
    passed = _check_gate(findings, gate)
    return ScanReport(
        repo_root=str(repo_root),
        repo_key=repo_key,
        started_at=started,
        duration_seconds=duration,
        findings=findings,
        gate=gate,
        passed_gate=passed,
    )


# --------------------------------------------------------- finding collectors


def _collect_drift(repo_key: str, baseline_path: str | Path | None) -> list[ScanFinding]:
    """Phase-6 drift detector run as a scan source."""
    from asil_drift import DriftDetector
    from asil_memory import GraphStore

    out: list[ScanFinding] = []
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except Exception as exc:
        out.append(
            ScanFinding(
                rule_id="scan/graph-unavailable",
                severity=Severity.warning,
                message=f"graph store unreachable; drift skipped ({exc})",
            )
        )
        return out

    try:
        baseline = _load_baseline(baseline_path, repo_key)
        events = DriftDetector(graph_store=gstore).detect(repo_key, baseline)
        for ev in events:
            out.append(
                ScanFinding(
                    rule_id=f"drift/{ev.kind}",
                    severity=_drift_severity(ev.severity, ev.kind),
                    message=ev.description or f"{ev.caller} -> {ev.callee}",
                    file_path=None,
                    line=None,
                    extra={
                        "caller": ev.caller,
                        "callee": ev.callee,
                        "boundary_name": ev.boundary_name,
                        "raw_severity": ev.severity,
                    },
                )
            )
    except Exception as exc:
        out.append(
            ScanFinding(
                rule_id="scan/drift-error",
                severity=Severity.warning,
                message=f"drift detector failed: {exc}",
            )
        )
    finally:
        gstore.close()
    return out


def _collect_recent_incident_causes(repo_key: str, *, lookback_hours: int) -> list[ScanFinding]:
    """For every incident in the last `lookback_hours`, surface a `note`
    finding listing the top causal candidates that point at code in
    this repo.

    This is the "what production has been telling you" signal — the
    moat made consumable by CI. A reviewer reading the PR comment sees
    "by the way, the auth-cascade incident on 2026-04-12 had a deploy
    of THIS file as its top cause; double-check your changes here."
    """
    from asil_memory import GraphStore

    cypher = (
        "MATCH (i:Incident) "
        "WHERE i.detected_at IS NOT NULL "
        "  AND duration.between(datetime(i.detected_at), datetime()).hours <= $h "
        "OPTIONAL MATCH (c)-[r:PRECEDED]->(i) "
        "WITH i, collect({kind: labels(c)[0], "
        "                 confidence: r.confidence, "
        "                 strategy: r.strategy, "
        "                 derivation: r.derivation, "
        "                 props: properties(c)}) AS causes "
        "RETURN i.id AS incident_id, i.summary AS summary, "
        "       i.detected_at AS detected_at, "
        "       i.severity AS severity, causes "
        "ORDER BY i.detected_at DESC LIMIT 20"
    )

    out: list[ScanFinding] = []
    try:
        gstore = GraphStore()
        gstore.verify_connectivity()
    except Exception:
        return out
    try:
        rows = gstore.query(cypher, h=lookback_hours)
    except Exception:
        rows = []
    finally:
        gstore.close()

    for row in rows:
        causes = [c for c in (row.get("causes") or []) if c.get("kind")]
        causes.sort(key=lambda c: float(c.get("confidence") or 0), reverse=True)
        if not causes:
            continue
        top = causes[0]
        out.append(
            ScanFinding(
                rule_id="incident/recent-cause",
                severity=Severity.note,
                message=(
                    f"incident {row['incident_id']} (last {lookback_hours}h): "
                    f"top cause = {top.get('kind')} "
                    f"(strategy={top.get('strategy')}, "
                    f"confidence={float(top.get('confidence') or 0):.2f})"
                ),
                derivation=[c.get("derivation", "") for c in causes[:3]],
                extra={
                    "incident_id": row["incident_id"],
                    "incident_summary": row.get("summary"),
                    "severity_in_runtime": row.get("severity"),
                    "top_causes": causes[:3],
                },
            )
        )
    return out


def _drift_severity(raw: str | None, kind: str) -> Severity:
    """Map an `asil_drift` severity string + event kind onto our `Severity`
    enum. Boundary violations escalate to `critical` since they cross
    architectural lines the team explicitly drew."""
    if kind == "boundary_violation":
        return Severity.critical
    table = {
        "critical": Severity.error,
        "warning": Severity.warning,
        "info": Severity.note,
    }
    return table.get((raw or "").lower(), Severity.warning)


def _load_baseline(path: str | Path | None, repo_key: str):
    """Load a baseline JSON file if one was given. Otherwise return an
    empty baseline so every observed dependency reads as 'new' — which
    is the right behavior for first-run scans."""
    from asil_drift.models import BaselineSnapshot

    if path is None:
        return BaselineSnapshot(repo_key=repo_key)
    p = Path(path)
    if not p.exists():
        return BaselineSnapshot(repo_key=repo_key)
    import json

    raw = json.loads(p.read_text(encoding="utf-8"))
    return BaselineSnapshot(
        repo_key=raw.get("repo_key", repo_key),
        captured_at=raw.get("captured_at"),
        dependencies=set(tuple(d) for d in raw.get("dependencies", [])),
        services=raw.get("services", []),
        function_count=raw.get("function_count", 0),
    )


# ----------------------------------------------------------------- quality gate


def _check_gate(findings: list[ScanFinding], gate: str) -> bool:
    """Returns True if the gate passed.

    Gate semantics, designed to behave like SonarQube quality gates:

      - `strict`:  any finding at warning or above -> fail.
      - `normal`:  any finding at error or above -> fail. (default)
      - `lenient`: only `critical` -> fail.
      - `none`:    always passes; CI surfaces findings as information only.
    """
    if gate == "none":
        return True
    thresholds = {
        "strict": _SEVERITY_RANK[Severity.warning],
        "normal": _SEVERITY_RANK[Severity.error],
        "lenient": _SEVERITY_RANK[Severity.critical],
    }
    floor = thresholds.get(gate, thresholds["normal"])
    return not any(_SEVERITY_RANK[f.severity] >= floor for f in findings)
