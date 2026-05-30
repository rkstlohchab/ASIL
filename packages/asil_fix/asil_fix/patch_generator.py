"""Patch generator — the constrained autonomous coder.

The LLM is intentionally given a *narrow* slice of context:
  - The incident summary.
  - The top causal candidates (with strategy, confidence, derivation).
  - The text of the specific functions / files implicated by the cause.

It is NOT given the whole repo. It is NOT asked "what should we fix?"
in the abstract. It is told "here is observable evidence that X caused
Y; emit a minimal unified diff that addresses X" — and the diff is
later validated by `git apply --check` before any sandbox even sees it.

This is what separates ASIL's fix pipeline from a generic coding agent:
the LLM is the *executor* of a hypothesis the deterministic causal
linker already settled on, not the *author* of the hypothesis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asil_core.llm import ModelRouter
from asil_core.logging import get_logger
from asil_memory import GraphStore
from asil_replay import ReplayEngine

from asil_fix.models import FixProposal

log = get_logger(__name__)


@dataclass(slots=True)
class _CodeContext:
    """One chunk of code paired with the cause that pointed at it."""

    file_path: str
    function_qname: str | None
    body: str
    cause_kind: str
    cause_confidence: float
    cause_derivation: str


_DIFF_FENCE_RE = re.compile(r"```(?:diff|patch)?\n(.*?)\n```", re.DOTALL)
_BARE_DIFF_RE = re.compile(r"(--- [^\n]+\n\+\+\+ [^\n]+\n@@.*)", re.DOTALL)


class PatchGenerator:
    """Generates a `FixProposal` from a Phase-5 replay result.

    Stateless — one instance per process. Pass the `ReplayResult` (or
    just an incident_id and let the generator load it via the GraphStore)
    plus the repo root, get back a `FixProposal` whose `diff` field is
    ready to feed to `git apply` or a sandbox.
    """

    def __init__(
        self,
        *,
        router: ModelRouter,
        graph_store: GraphStore,
        max_context_files: int = 4,
        max_context_chars_per_file: int = 2_000,
    ) -> None:
        self._router = router
        self._graph = graph_store
        self._max_files = max_context_files
        self._max_chars = max_context_chars_per_file

    async def propose(
        self,
        *,
        incident_id: str,
        repo_root: str | Path,
        repo_key: str | None = None,
    ) -> FixProposal:
        """Build a proposal for `incident_id`. Raises ValueError if no
        causal chain exists for the incident (the moat must run first)."""
        repo_root = Path(repo_root).resolve()
        engine = ReplayEngine(graph_store=self._graph)
        replay = engine.replay(incident_id, causes_limit=5)
        if replay is None:
            raise ValueError(f"no replay for {incident_id!r} — ingest the postmortem first")
        if not replay.top_causes:
            raise ValueError(
                f"no causal chain for {incident_id!r} — run `asil temporal link` first"
            )

        context = self._gather_context(replay.top_causes, repo_root, repo_key)
        prompt = self._build_prompt(replay, context)

        resp = await self._router.call(
            tier="reasoning",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2048,
        )

        diff = _extract_diff(resp.text)
        affected = _files_touched_by_diff(diff)
        confidence_score = _aggregate_confidence(replay.top_causes, replay.confidence.score)
        derivation = [
            "patch generator constrained to top-5 causal candidates",
            f"top cause: {replay.top_causes[0].get('cause_kind')} "
            f"(strategy={replay.top_causes[0].get('strategy')}, "
            f"confidence={replay.top_causes[0].get('confidence'):.3f})",
            f"context: {len(context)} file(s), {sum(len(c.body) for c in context)} chars",
            f"model={resp.model} provider={resp.provider} cost=${resp.cost_usd:.6f}",
        ]
        if not diff:
            derivation.append("WARNING: LLM did not emit a parseable diff")

        return FixProposal(
            incident_id=incident_id,
            summary=_first_diff_line_summary(diff) or "no parseable diff",
            diff=diff,
            affected_files=affected,
            causal_chain=list(replay.top_causes),
            confidence_score=confidence_score,
            derivation=derivation,
            model=resp.model,
            cost_usd=resp.cost_usd,
            generated_at=datetime.now(UTC),
            repo_key=repo_key,
        )

    # ---------------------------------------------------------------- internals

    def _gather_context(
        self,
        causes: list[dict[str, Any]],
        repo_root: Path,
        repo_key: str | None,
    ) -> list[_CodeContext]:
        """For each top cause, pull the implicated file's body (truncated).

        We look at three sources for file paths, in order:
          1. `cause.cause_props.file_path` (set by some adapter ingestions).
          2. `cause.cause_props.service_name` -> Service.file_paths.
          3. Skip — the generator surfaces what it has, even if narrow.
        """
        out: list[_CodeContext] = []
        seen: set[str] = set()
        for cause in causes[: self._max_files]:
            props = cause.get("cause_props", {}) or {}
            file_path = props.get("file_path")
            if not file_path and (svc_name := props.get("service_name")):
                file_path = self._lookup_service_file(svc_name, repo_key)
            if not file_path or file_path in seen:
                continue
            seen.add(file_path)
            text = _safe_read_truncated(repo_root, file_path, self._max_chars)
            if text is None:
                continue
            out.append(
                _CodeContext(
                    file_path=file_path,
                    function_qname=props.get("qualified_name"),
                    body=text,
                    cause_kind=cause.get("cause_kind", "?"),
                    cause_confidence=float(cause.get("confidence", 0.0)),
                    cause_derivation=cause.get("derivation", ""),
                )
            )
        return out

    def _lookup_service_file(self, service_name: str, repo_key: str | None) -> str | None:
        """Best-effort: find one file owned by the service from the runtime
        graph's `Service.file_paths` property."""
        cypher = "MATCH (s:Service {name: $name}) RETURN s.file_paths AS files LIMIT 1"
        try:
            rows = self._graph.query(cypher, name=service_name)
        except Exception:
            return None
        if not rows:
            return None
        files = rows[0].get("files") or []
        return files[0] if files else None

    def _build_prompt(self, replay, context: list[_CodeContext]) -> str:
        causes_md = "\n".join(
            f"- {c.get('cause_kind')} "
            f"[strategy={c.get('strategy')}, "
            f"confidence={c.get('confidence', 0):.3f}, "
            f"delta_seconds={c.get('delta_seconds', 0):.1f}]: "
            f"{c.get('derivation', '')}"
            for c in replay.top_causes
        )
        context_md = "\n\n".join(
            f"### {c.file_path}"
            + (f" — `{c.function_qname}`" if c.function_qname else "")
            + f"\n(implicated by {c.cause_kind}, "
            f"confidence={c.cause_confidence:.2f})\n"
            f"```\n{c.body}\n```"
            for c in context
        )
        if not context_md:
            context_md = "_(no code context available — operate on the causes alone)_"

        return (
            f"## Incident\n"
            f"- id: {replay.incident_id}\n"
            f"- summary: {replay.incident.get('summary', '(none)')}\n"
            f"- severity: {replay.incident.get('severity', 'unknown')}\n\n"
            f"## Observable causal chain (Phase 4 output)\n{causes_md}\n\n"
            f"## Implicated code\n{context_md}\n\n"
            f"## Required output\n"
            f"A single unified diff that, applied to the repo root, addresses "
            f"the top causal candidate (not the symptom). Keep the change "
            f"minimal — touch only the implicated file(s). If you cannot "
            f"determine a fix from the evidence above, output a diff that "
            f"adds a clear TODO comment with the rationale; never invent "
            f"new files."
        )


_SYSTEM_PROMPT = (
    "You are ASIL's constrained patch generator. Your only job is to emit "
    "a minimal `git apply`-compatible unified diff that addresses the "
    "TOP causal candidate ASIL's deterministic linker already identified. "
    "Do not redesign. Do not refactor. Touch only the file(s) explicitly "
    "listed in the 'Implicated code' section. Output ONLY the diff, "
    "wrapped in a ```diff fence."
)


# ---------------------------------------------------------------------- helpers


def _extract_diff(text: str) -> str:
    """Pull the diff body out of the LLM response. Tolerates both fenced
    (```diff ... ```) and bare-diff (--- a/... +++ b/...) formats."""
    if not text:
        return ""
    m = _DIFF_FENCE_RE.search(text)
    if m:
        return m.group(1).strip() + "\n"
    m = _BARE_DIFF_RE.search(text)
    if m:
        return m.group(1).strip() + "\n"
    return ""


def _files_touched_by_diff(diff: str) -> list[str]:
    """Extract the b/<path> filenames from a unified diff."""
    if not diff:
        return []
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[len("+++ b/") :].strip())
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            files.append(line[len("+++ ") :].strip())
    # dedupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _aggregate_confidence(causes: list[dict[str, Any]], replay_confidence: float) -> float:
    """Combine top-cause confidence with the overall replay confidence.

    A high-confidence replay built on shaky causes shouldn't get a
    high-confidence fix. We take the minimum so the weakest link bounds
    the proposal — this is intentional cause-vs-symptom honesty.
    """
    if not causes:
        return 0.0
    top = float(causes[0].get("confidence", 0.0) or 0.0)
    return round(min(top, replay_confidence), 4)


def _first_diff_line_summary(diff: str) -> str | None:
    """Best-effort 1-line headline for a diff (filename + first hunk header)."""
    if not diff:
        return None
    for line in diff.splitlines():
        if line.startswith("+++ b/") or line.startswith("+++ "):
            return f"patches {line.split(' ', 1)[1]}"
    return diff.splitlines()[0][:120]


def _safe_read_truncated(repo_root: Path, rel_path: str, limit: int) -> str | None:
    """Read a file's text, truncated to `limit` chars. Returns None on any
    error so the generator can fall through to the next candidate."""
    try:
        path = (repo_root / rel_path).resolve()
        if not path.is_relative_to(repo_root.resolve()):
            return None  # escape attempt; never read outside the repo
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > limit:
            return text[:limit] + f"\n... [truncated at {limit} chars]"
        return text
    except (OSError, UnicodeDecodeError):
        return None
