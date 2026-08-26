---
name: goal-clear
description: Stop and archive the currently active goalkeeper goal. Use when the user invokes /goal-clear to abandon or finalize a goal. Files are moved to .claude/goals/_archive/, never deleted.
---

You are operating the **goal-clear** skill. The gk CLI is `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gk.py"`, abbreviated `gk`.

## Flow

1. Run `gk status` to see the active goal and its status. No active goal → tell the user and stop.
2. **Confirm via AskUserQuestion** before archiving (unless the goal is already `done` — then clear is just bookkeeping):
   - **Archive and clear (Recommended)** — proceed.
   - **Cancel** — abort, no changes.
3. Run `gk clear --yes`. gk appends the final log entry, moves the goal directory to `.claude/goals/_archive/<slug>-<timestamp>/`, writes the terminal `active.json`, and — if a chain is in flight — marks it `aborted` without advancing (unreached links' contracts stay in place for re-use).
4. Relay gk's output: the archive path, and that `/goal` or `/goal-prep` starts a new goal.

## Hard rules

- **Never delete files.** gk always archives; nothing is removed.
- **Never run `gk clear --yes` without the user's confirmation** (except for an already-`done` goal). The `--yes` flag exists so gk knows the question was asked — it is not a license to skip it.
- **Clearing kills any active chain** — chains are not auto-resumed mid-stream.
