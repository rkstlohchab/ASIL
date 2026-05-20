---
name: asil-positioning
description: Use when writing user-facing copy — README, docs, devlog posts, commit messages, PR descriptions, demo scripts, slide content. Enforces ASIL's positioning as Engineering Intelligence Infrastructure and forbids commodity coding-agent framing.
---

# asil-positioning

ASIL's positioning is load-bearing. Drifting into "another AI coder" framing in docs is a real risk; readers slot the project into the OpenHands / Cursor / Devin bucket and the differentiation evaporates.

## Use these terms

- **Engineering Intelligence Infrastructure** ← canonical
- Persistent / temporal / causal layer underneath coding agents
- Temporal causality engine
- Execution replay (or: time-travel debugging for distributed systems)
- Confidence-scored reasoning
- Architecture drift detection
- Knowledge graph of the engineering org

## Don't use these terms

| ❌ Wrong | ✅ Replace with |
|---|---|
| "AI OS" / "AI operating system" | "engineering intelligence infrastructure" |
| "Autonomous coder" / "AI engineer" | "underneath coding agents" |
| "AI coding assistant" / "AI pair programmer" | (describe the capability instead) |
| "Chatbot for engineers" | (drop entirely) |
| "Copilot for X" | (drop entirely) |
| "Agent that codes for you" | "agents query ASIL's knowledge graph" |
| "Replaces engineers" | "augments engineers with persistent context" |

## The hero sentence

When you have one line to describe ASIL, use a variant of:

> *"Why did this production incident happen?" — ASIL reconstructs the timeline, identifies the root cause with a confidence score and evidence list, and shows how the failure cascaded across services.*

## Differentiation framing

ASIL's defensible niche is *underneath* coding agents, not next to them.

- OpenHands, Cursor, Claude Code, Aider → file edits + commands.
- ASIL → persistent knowledge graph + temporal causality + replay + drift.
- Coding agents become **MCP clients** of ASIL.

When mentioning prior art, lead with what ASIL adds, not what's missing in others.

## The four pillars (use in feature lists)

1. Temporal causality
2. Execution replay
3. Confidence-scored reasoning
4. Architecture drift detection

## Commit message style

```
feat(asil_temporal): causal linker with lagged Pearson correlation
fix(asil_replay): off-by-one in cascade traversal hop count
docs: clarify confidence scorer pre-Phase 2
chore(deps): bump tree-sitter to 0.24
```

Never write:
- "feat: agent files PRs"
- "feat: AI fixes bugs"
- "chore: improve copilot UX"

Write the *capability*, not the framing.

## PR-filing is Phase 8 stretch

Don't lead any external comms with autonomous PR-filing. That's commodity work and pushes us back into the crowded space. If the user demos ASIL, the headline is incident root-cause reconstruction, not "look it wrote a PR."
