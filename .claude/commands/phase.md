---
description: Show ASIL's current phase, eval bar, and what's left before the next gate
---

Read [PLAN.md](../../PLAN.md) and report concisely (under 250 words):

1. **Current phase** — the number, name, and goal (one sentence).
2. **Demoable artifact** — what the phase's demo must show.
3. **Eval bar** — the row from the Evaluation table that gates moving to the next phase.
4. **Status of items** — for each line under the current phase, mark it as ✅ done / ◐ in-progress / ⬜ not started, inferring from what's in the working tree (file existence, git log, tests passing).
5. **Next gate** — what concretely has to ship before the phase passes.
6. **What to defer** — if any open work belongs to a later phase, flag it explicitly.

Use the actual repo state, not memory. Cite file paths for items you check.

Do NOT propose to start the next phase yet — that's the user's call.
