"""SARIF 2.1.0 emitter for `asil scan` output.

SARIF is the standard CI tools (GitHub code scanning, SonarQube,
Semgrep, every static analyser worth deploying) consume. Emitting it
means ASIL's findings land in the same UI as the rest of your linter
output, without any custom integration.

We map our `ScanFinding` model onto a single SARIF `run` with one
`tool.driver` (us) and one `result` per finding. The rule descriptors
are reconstructed lazily from the set of `rule_id`s actually present
in this scan — there's no need to ship a full rule catalog when half
of it never fires.

Spec: https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html
"""

from __future__ import annotations

from typing import Any

from asil_eval.scan import ScanFinding, ScanReport, Severity

_SEVERITY_TO_SARIF_LEVEL = {
    Severity.critical: "error",
    Severity.error: "error",
    Severity.warning: "warning",
    Severity.note: "note",
}

# Rule descriptors. Keys are the `rule_id` strings emitted by `scan.py`.
# A rule that fires without an entry here still renders cleanly — SARIF
# tolerates undefined rules — but a `helpUri` + a longer help text makes
# the GitHub code-scanning UI much friendlier.
_RULE_CATALOG: dict[str, dict[str, str]] = {
    "drift/boundary_violation": {
        "shortDescription": "Boundary violation",
        "fullDescription": (
            "A new dependency crosses an architectural boundary that was "
            "stable in the baseline. This is a critical drift signal — "
            "the team explicitly drew this line, and the change crosses it."
        ),
        "helpUri": "https://github.com/rkstlohchab/ASIL/blob/main/PLAN.md#phase-6",
    },
    "drift/new_dependency": {
        "shortDescription": "New dependency edge",
        "fullDescription": (
            "A call edge that did not exist in the stored baseline. Not "
            "automatically wrong — most refactors add edges — but worth "
            "a glance before merging."
        ),
        "helpUri": "https://github.com/rkstlohchab/ASIL/blob/main/PLAN.md#phase-6",
    },
    "drift/removed_dependency": {
        "shortDescription": "Removed dependency edge",
        "fullDescription": (
            "A call edge that was in the baseline is gone. Often fine "
            "(dead-code removal); sometimes a sign of accidental breakage."
        ),
    },
    "incident/recent-cause": {
        "shortDescription": "Recent production incident touching related code",
        "fullDescription": (
            "ASIL's Phase-4 causal linker identified a recent incident "
            "whose top cause points at code in this repo. Reviewer should "
            "double-check whether the current change addresses or "
            "regresses the root cause."
        ),
        "helpUri": "https://github.com/rkstlohchab/ASIL/blob/main/PLAN.md#phase-4",
    },
    "scan/graph-unavailable": {
        "shortDescription": "Graph store unavailable during scan",
        "fullDescription": (
            "Neo4j was not reachable when the scan ran; drift detection "
            "was skipped. Treat as a warning — not a quality finding, "
            "but a CI configuration issue worth surfacing."
        ),
    },
    "scan/drift-error": {
        "shortDescription": "Drift detector raised",
        "fullDescription": "An exception escaped the drift detector. See message.",
    },
}


def to_sarif(report: ScanReport) -> dict[str, Any]:
    """Return a SARIF-shaped dict. Callers stringify it with `json.dumps`."""
    rules_used = sorted({f.rule_id for f in report.findings})
    rules = [_rule_descriptor(rid) for rid in rules_used]

    results = [_finding_to_result(f) for f in report.findings]

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ASIL",
                        "fullName": "ASIL Engineering Intelligence Infrastructure",
                        "informationUri": "https://github.com/rkstlohchab/ASIL",
                        "version": "0.0.1",
                        "rules": rules,
                    }
                },
                "invocations": [
                    {
                        "executionSuccessful": report.passed_gate,
                        "startTimeUtc": report.started_at.isoformat(),
                        "exitCode": 0 if report.passed_gate else 1,
                        "properties": {
                            "gate": report.gate,
                            "duration_seconds": report.duration_seconds,
                            "repo_key": report.repo_key,
                        },
                    }
                ],
                "results": results,
                "properties": report.counts,
            }
        ],
    }


def _rule_descriptor(rule_id: str) -> dict[str, Any]:
    base = _RULE_CATALOG.get(rule_id, {})
    out: dict[str, Any] = {"id": rule_id}
    if "shortDescription" in base:
        out["shortDescription"] = {"text": base["shortDescription"]}
    if "fullDescription" in base:
        out["fullDescription"] = {"text": base["fullDescription"]}
    if "helpUri" in base:
        out["helpUri"] = base["helpUri"]
    return out


def _finding_to_result(f: ScanFinding) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ruleId": f.rule_id,
        "level": _SEVERITY_TO_SARIF_LEVEL[f.severity],
        "message": {"text": f.message},
    }
    if f.file_path:
        loc: dict[str, Any] = {
            "physicalLocation": {
                "artifactLocation": {"uri": f.file_path},
            }
        }
        if f.line:
            loc["physicalLocation"]["region"] = {"startLine": f.line}
        out["locations"] = [loc]
    if f.derivation:
        # SARIF supports `relatedLocations` for context; we encode the
        # derivation trail as a single related-location message bundle.
        out["properties"] = {"derivation": f.derivation}
    if f.extra:
        out.setdefault("properties", {}).update(f.extra)
    return out
