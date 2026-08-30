---
name: goal-resume
description: Resume a paused or needs_human goalkeeper goal. Use when the user invokes /goal-resume after they've manually unblocked the goal (e.g. fixed a problem the judge flagged).
---

You are operating the **goal-resume** skill. The gk CLI is `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gk.py"`, abbreviated `gk`.

## Flow

1. Run `gk status`. It shows the active goal (if any) and the **Parked** queue — parked goals hold no slot, so "no active goal" does not mean "nothing to resume." If the user named a goal, target it; otherwise the active goal, or the single parked goal if that is unambiguous. Nothing to resume → tell the user and stop.
2. Branch on the target's status:
   - `active` — already running; `gk resume` will say so. No-op.
   - `done` — goals don't reopen; suggest `/goal-clear` to archive, then a new contract.
   - `paused` — run `gk resume --keep-count`.
   - `needs_human`, rejection count 0 (parked on a human-gated blocker) — confirm the recorded `needs` is actually provided, then `gk resume <slug>`.
   - `needs_human`, rejections > 0 (judge rejected too many times) — **Ask the user via AskUserQuestion** whether to:
     - **Reset rejection counter and continue (Recommended)** — user confirms they fixed the flagged issues → `gk resume <slug> --reset-rejections`.
     - **Continue without reset** — one more rejection re-parks it → `gk resume <slug> --keep-count`.
     - **Abandon** — route to `/goal-clear`.

   gk refuses a bare `resume` in that case precisely so this question cannot be skipped.
3. gk restores the slot and, if the goal heads a `waiting` chain, re-arms the chain. If another goal holds the slot, gk refuses — finish, park, or clear that one first (one loop at a time).
4. On success, **re-enter the goal skill execution loop immediately.** Do not just schedule a wakeup — do real work in this turn. If the goal is a chain link (see `gk status`), re-spawn the executor subagent per `goal-chain/SKILL.md` instead.

## Hard rules

- Never silently reset the rejection counter — the AskUserQuestion step is mandatory for needs_human (and gk enforces the explicit flag).
- Never resume a `done` goal.
