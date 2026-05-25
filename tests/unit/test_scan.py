"""Unit tests for `asil scan` — the CI entry point.

We exercise the gate logic, the SARIF emitter, and the PR-comment
renderer in isolation. The orchestrator's I/O paths (drift detector,
Neo4j query) are exercised via the live integration tests; here we use
hand-rolled `ScanReport`s so each scenario is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from asil_eval import to_pr_comment, to_sarif
from asil_eval.scan import (
    ScanFinding,
    ScanReport,
    Severity,
    _check_gate,
    _drift_severity,
)


def _report(findings: list[ScanFinding], *, gate: str = "normal") -> ScanReport:
    return ScanReport(
        repo_root="/tmp/test",
        repo_key="local:/tmp/test",
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        duration_seconds=1.23,
        findings=findings,
        gate=gate,
        passed_gate=_check_gate(findings, gate),
    )


# ----------------------------------------------------------------- gate logic


def test_gate_none_always_passes_even_with_critical():
    findings = [
        ScanFinding(rule_id="x", severity=Severity.critical, message="boom")
    ]
    report = _report(findings, gate="none")
    assert report.passed_gate is True


def test_gate_lenient_fires_only_on_critical():
    findings = [ScanFinding(rule_id="x", severity=Severity.error, message="boom")]
    assert _report(findings, gate="lenient").passed_gate is True
    findings = [ScanFinding(rule_id="x", severity=Severity.critical, message="boom")]
    assert _report(findings, gate="lenient").passed_gate is False


def test_gate_normal_fires_on_error_and_critical():
    assert _report(
        [ScanFinding(rule_id="x", severity=Severity.warning, message="m")],
        gate="normal",
    ).passed_gate is True
    assert _report(
        [ScanFinding(rule_id="x", severity=Severity.error, message="m")],
        gate="normal",
    ).passed_gate is False
    assert _report(
        [ScanFinding(rule_id="x", severity=Severity.critical, message="m")],
        gate="normal",
    ).passed_gate is False


def test_gate_strict_fires_on_warning_and_above():
    assert _report(
        [ScanFinding(rule_id="x", severity=Severity.note, message="m")],
        gate="strict",
    ).passed_gate is True
    assert _report(
        [ScanFinding(rule_id="x", severity=Severity.warning, message="m")],
        gate="strict",
    ).passed_gate is False


def test_empty_findings_always_pass():
    for gate in ("strict", "normal", "lenient", "none"):
        assert _report([], gate=gate).passed_gate is True


def test_unknown_gate_falls_back_to_normal():
    assert _report(
        [ScanFinding(rule_id="x", severity=Severity.warning, message="m")],
        gate="bogus",
    ).passed_gate is True
    assert _report(
        [ScanFinding(rule_id="x", severity=Severity.error, message="m")],
        gate="bogus",
    ).passed_gate is False


# ----------------------------------------------------------- severity mapping


def test_boundary_violation_escalates_to_critical():
    assert _drift_severity("warning", "boundary_violation") is Severity.critical
    # Even an asil_drift `info` boundary violation should be critical.
    assert _drift_severity("info", "boundary_violation") is Severity.critical


def test_drift_severity_falls_back_to_warning_on_unknown():
    assert _drift_severity(None, "new_dependency") is Severity.warning
    assert _drift_severity("bizarre", "new_dependency") is Severity.warning


def test_drift_severity_maps_known_levels():
    assert _drift_severity("critical", "new_dependency") is Severity.error
    assert _drift_severity("warning", "new_dependency") is Severity.warning
    assert _drift_severity("info", "new_dependency") is Severity.note


# -------------------------------------------------------------- report counts


def test_report_counts_buckets_correctly():
    findings = [
        ScanFinding(rule_id="a", severity=Severity.critical, message=""),
        ScanFinding(rule_id="b", severity=Severity.error, message=""),
        ScanFinding(rule_id="c", severity=Severity.warning, message=""),
        ScanFinding(rule_id="d", severity=Severity.warning, message=""),
        ScanFinding(rule_id="e", severity=Severity.note, message=""),
    ]
    counts = _report(findings).counts
    assert counts == {
        "critical": 1,
        "error": 1,
        "warning": 2,
        "note": 1,
    }


def test_max_severity_picks_the_top():
    findings = [
        ScanFinding(rule_id="a", severity=Severity.warning, message=""),
        ScanFinding(rule_id="b", severity=Severity.critical, message=""),
        ScanFinding(rule_id="c", severity=Severity.note, message=""),
    ]
    assert _report(findings).max_severity is Severity.critical


def test_max_severity_is_none_when_empty():
    assert _report([]).max_severity is None


# ---------------------------------------------------------------- SARIF shape


def test_sarif_top_level_keys_are_v2_1_0_compliant():
    report = _report(
        [
            ScanFinding(
                rule_id="drift/boundary_violation",
                severity=Severity.critical,
                message="auth -> payment",
                file_path="src/auth.py",
                line=42,
                extra={"caller": "auth", "callee": "payment"},
            )
        ]
    )
    out = to_sarif(report)
    assert out["version"] == "2.1.0"
    assert len(out["runs"]) == 1
    run = out["runs"][0]
    assert run["tool"]["driver"]["name"] == "ASIL"
    assert run["invocations"][0]["executionSuccessful"] is False
    assert run["invocations"][0]["exitCode"] == 1


def test_sarif_result_level_maps_severity_correctly():
    findings = [
        ScanFinding(rule_id="a", severity=Severity.critical, message=""),
        ScanFinding(rule_id="b", severity=Severity.error, message=""),
        ScanFinding(rule_id="c", severity=Severity.warning, message=""),
        ScanFinding(rule_id="d", severity=Severity.note, message=""),
    ]
    out = to_sarif(_report(findings))
    levels = [r["level"] for r in out["runs"][0]["results"]]
    assert levels == ["error", "error", "warning", "note"]


def test_sarif_includes_location_when_file_path_set():
    findings = [
        ScanFinding(
            rule_id="x",
            severity=Severity.warning,
            message="m",
            file_path="src/a.py",
            line=10,
        )
    ]
    out = to_sarif(_report(findings))
    loc = out["runs"][0]["results"][0]["locations"][0]
    assert loc["physicalLocation"]["artifactLocation"]["uri"] == "src/a.py"
    assert loc["physicalLocation"]["region"]["startLine"] == 10


def test_sarif_omits_locations_when_no_file_path():
    findings = [ScanFinding(rule_id="x", severity=Severity.note, message="")]
    out = to_sarif(_report(findings))
    assert "locations" not in out["runs"][0]["results"][0]


def test_sarif_includes_rule_descriptors_for_known_rules():
    findings = [
        ScanFinding(rule_id="drift/boundary_violation", severity=Severity.critical, message="")
    ]
    out = to_sarif(_report(findings))
    rules = out["runs"][0]["tool"]["driver"]["rules"]
    assert rules[0]["id"] == "drift/boundary_violation"
    assert "shortDescription" in rules[0]
    assert "helpUri" in rules[0]


def test_sarif_passing_gate_marks_execution_successful():
    out = to_sarif(_report([]))
    assert out["runs"][0]["invocations"][0]["executionSuccessful"] is True
    assert out["runs"][0]["invocations"][0]["exitCode"] == 0


# --------------------------------------------------------- PR-comment shape


def test_pr_comment_has_badge_and_counts_line():
    findings = [
        ScanFinding(rule_id="a", severity=Severity.error, message="boom"),
        ScanFinding(rule_id="b", severity=Severity.warning, message="meh"),
    ]
    md = to_pr_comment(_report(findings))
    assert "## ASIL scan" in md
    assert "**failed**" in md
    assert "1 errors" in md
    assert "1 warnings" in md


def test_pr_comment_says_passed_when_empty():
    md = to_pr_comment(_report([]))
    assert "**passed**" in md
    assert "0 critical" in md


def test_pr_comment_includes_finding_rule_id_and_message():
    findings = [
        ScanFinding(
            rule_id="drift/boundary_violation",
            severity=Severity.critical,
            message="auth crosses into payment internals",
        )
    ]
    md = to_pr_comment(_report(findings))
    assert "`drift/boundary_violation`" in md
    assert "auth crosses into payment internals" in md


def test_pr_comment_groups_by_severity_with_details():
    findings = [
        ScanFinding(rule_id="a", severity=Severity.critical, message="x"),
        ScanFinding(rule_id="b", severity=Severity.note, message="y"),
    ]
    md = to_pr_comment(_report(findings))
    # Both severity tiers should appear as collapsible <details> blocks.
    assert md.count("<details>") == 2


def test_pr_comment_ends_with_attribution_link():
    md = to_pr_comment(_report([]))
    assert "[ASIL](https://github.com/rkstlohchab/ASIL)" in md
