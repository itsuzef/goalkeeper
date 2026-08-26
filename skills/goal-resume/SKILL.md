---
name: goal-resume
description: Resume a paused or needs_human goalkeeper goal. Use when the user invokes /goal-resume after they've manually unblocked the goal (e.g. fixed a problem the judge flagged).
---

You are operating the **goal-resume** skill. The gk CLI is `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gk.py"`, abbreviated `gk`.

## Flow

1. Run `gk status` to see the active goal and its status. No active goal → tell the user and stop.
2. Branch on status:
   - `active` — already running; `gk resume` will say so. No-op.
   - `done` — goals don't reopen; suggest `/goal-clear` to archive, then a new contract.
   - `paused` — run `gk resume --keep-count`.
   - `needs_human` — the judge rejected too many times. **Ask the user via AskUserQuestion** whether to:
     - **Reset rejection counter and continue (Recommended)** — user confirms they fixed the flagged issues → `gk resume --reset-rejections`.
     - **Continue without reset** — one more rejection re-triggers needs_human → `gk resume --keep-count`.
     - **Abandon** — route to `/goal-clear`.

   gk refuses a bare `resume` from needs_human precisely so this question cannot be skipped.
3. On success, **re-enter the goal skill execution loop immediately.** Do not just schedule a wakeup — do real work in this turn. If the goal is a chain link (see `gk status`), re-spawn the executor subagent per `goal-chain/SKILL.md` instead.

## Hard rules

- Never silently reset the rejection counter — the AskUserQuestion step is mandatory for needs_human (and gk enforces the explicit flag).
- Never resume a `done` goal.
