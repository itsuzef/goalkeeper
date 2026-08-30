---
name: goal-pause
description: Pause the currently active goalkeeper goal without losing state. Use when the user invokes /goal-pause. The goal can later be resumed with /goal-resume.
---

You are operating the **goal-pause** skill.

## Flow

Run:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gk.py" pause
```

gk handles everything: it refuses cleanly if there is no active goal or the goal is already paused/done/needs_human, and otherwise sets `status = paused`, stamps `paused_at`, and appends the log entry. Relay its output to the user.

**Pause vs park:** pause means "I'll be right back" — the goal keeps the active slot. If the goal is actually waiting on a *person* (credential, consent, decision), use `gk park <slug> --needs "<what>"` instead: it frees the slot so other goals can run, and `gk status` carries the unblock instruction.

## Hard rules

- Do not cancel or alter any pending ScheduleWakeup. The wakeup prompt checks status via `gk status` and stops when not active, so a stale wakeup is a no-op.
- Do not touch any state beyond what `gk pause` writes.
