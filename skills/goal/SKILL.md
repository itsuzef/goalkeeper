---
name: goal
description: Set or check status of a durable goalkeeper goal. Use this skill when the user invokes /goal "<objective>" to start a new contract-driven goal, or /goal with no arguments to see status of the currently active goal. Goalkeeper goals run autonomously across many turns with checkpoint validation and judge-gated completion.
---

You are operating the **goalkeeper** skill — durable, contract-driven goal execution with judge-gated completion. This skill is invoked when the user runs `/goal` or `/goal "<objective>"`.

## The gk CLI — all state mechanics go through it

Every goalkeeper state transition is executed by the `gk` CLI, not by you writing files:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gk.py" <command>
```

Abbreviated below as `gk <command>`. Resolve `${CLAUDE_PLUGIN_ROOT}` to the goalkeeper plugin's root directory once and reuse the absolute path.

**Never hand-write `state.json`, `active.json`, `chain.json`, or append to `log.md` with Edit/Write.** The canonical state shapes live in `scripts/gk.py` (and are asserted by `scripts/test-gk.py`); a PreToolUse hook shipped with this plugin blocks direct edits to the active goal's `contract.md`, `log.md`, and `state.json` — the audit trail and contract immutability are mechanical guarantees, not requests. If a gk command refuses, it is telling you something about state — read its message; do not work around it by editing files.

Commands you will use here: `gk status`, `gk activate <slug>`, `gk checkpoint <slug> --message "..."`, `gk validate <slug>`, `gk log <slug> --compact`, `gk verdict` (via the goal-judge skill), `gk park <slug> --needs "..."` (human-gated blocker: frees the slot so other work continues), `gk doctor` (if state ever looks inconsistent).

## Execution modes

### Inline mode (standalone `/goal` / `/goal "<objective>"`)

Main conversation context runs the full Execution Loop (do work → checkpoint → validate → judge → branch on verdict), using ScheduleWakeup to pace iterations. This is the default when the user invokes `/goal` directly.

### Subagent mode (chain-driven via `/goal-chain`)

When `/goal-chain` orchestrates, each goal's implementation work runs in a **fresh-context executor subagent**; main context only orchestrates (spawn executor → spawn judge → apply verdict). This keeps main-context cost flat (~10K tokens per goal) so multi-goal chains complete in one session. See `goal-chain/SKILL.md` for the orchestration protocol.

The Execution Loop below applies in BOTH modes — the difference is who runs it: main context with ScheduleWakeup pacing (inline), or the executor subagent end-to-end in one turn (subagent). Both modes share the same on-disk state via gk.

## Decide mode from args

- `args` non-empty → **set mode** (start or resume a goal)
- `args` empty → **status mode** (report on active goal)

## Status mode

Run `gk status` and relay its output to the user. The `Parked` section is the human's unblock queue — each entry names exactly what a person must provide and the `gk resume <slug>` that restarts it. If a goal reports `NEEDS_HUMAN`, also surface the latest judge fix-list from the goal's log verbatim (`gk log <slug> --compact`).

## Set mode

1. **Derive slug:** extract or generate a kebab-case slug from the objective (≤64 chars, lowercase, alphanumeric and hyphen only). If the user passed an explicit `--slug=<value>`, use that.

2. **Contract resolution:**
   - If `.claude/goals/<slug>/contract.md` exists, treat the request as a resume: skip prep, jump to step 3.
   - If it does not exist, **auto-route to `/goal-prep`** with the same objective. Do not write a thin one-line contract — prep is mandatory because the contract IS the spec. After prep completes and writes the contract, return here.

3. **Activate:** run `gk activate <slug>`. This captures the git baseline (commit + dirty paths), merges any prep-captured validator baseline, writes the canonical state files, and logs the activation. If it refuses because another goal is active, surface that to the user — do not `--force` on your own judgment.

4. **Begin the execution loop** (next section).

## Execution loop

This block runs on activation AND on every ScheduleWakeup re-entry.

1. Orient: `gk status`, then read `contract.md` and `gk log <slug> --compact` to know where work left off.
2. If status is not `active`, stop. Do not schedule another wakeup.
3. **Do real work** on the objective for one checkpoint's worth of progress (size per `checkpoint_cadence` in the contract — e.g. ~5 file edits, ~20 minutes of effort, or one logical sub-task).
4. **Checkpoint:** `gk checkpoint <slug> --message "<one short paragraph: what changed, files touched, decisions made, what's next>"`.
5. **Run validator:** `gk validate <slug>`. Exit 0 = pass, exit 1 = fail. gk updates state and appends the log entry; the output tail is printed for you.
6. **Branch on validator:**
   - **Failed:**
     - *Inline mode:* schedule the next iteration via `ScheduleWakeup` (see Pacing below). Wakeup prompt:
       ```
       Continue active goalkeeper goal — run gk status and proceed per the goal skill execution loop.
       ```
     - *Subagent mode:* fix the failure and re-run `gk validate` (~3–5 inner attempts). If still failing, return to the chain orchestrator with `STATUS: validator_fail` + a clear BLOCKERS field per the executor directive in `goal-chain/SKILL.md`.
   - **Passed:**
     - *Inline mode:* invoke the **goal-judge** skill in this same turn. It assembles the brief with `gk judge-brief`, spawns the judge, and applies the verdict with `gk verdict`.
     - *Subagent mode:* do NOT invoke the judge. Return to the chain orchestrator with `STATUS: validator_pass` + the structured summary fields.
7. **Branch on judge outcome** *(inline mode — gk verdict prints the outcome)*:
   - **`DONE`** — the goal is complete and archived state is terminal. Surface a one-paragraph completion summary to the user.
   - **`NEXT: <slug>` / `CHAIN_COMPLETE`** — chain bookkeeping happened automatically; defer to the goal-chain skill's orchestration.
   - **`RETRY`** — judge rejected within budget. Schedule the next iteration with the fix-list as the primary task. Wakeup prompt:
     ```
     Continue active goalkeeper goal — judge rejected the last attempt. Read the most recent "judge rejected" block via gk log <slug> --compact and address each fix-list item. Then proceed per the goal skill execution loop.
     ```
   - **`NEEDS_HUMAN`** — max rejections reached. gk parked the goal: the active slot is free and the need is recorded in state. Do NOT schedule a wakeup for this goal. Surface the fix-list verbatim, point at `gk resume <slug> --reset-rejections` / `/goal-clear` — then **move on to other work.** The goal is parked, not you: a human-gated stop on one goal never idles the agent.

Mid-loop, if you hit a blocker only a human can clear (a credential, consent, account access, a decision that is the user's to make): `gk checkpoint` the exact blocker, then `gk park <slug> --needs "<exactly what a person must provide>"` and continue with other work. A blocker an agent could solve (missing tool, unclear code, failing environment) is work — solve it, never park it.

## Pacing wakeups

- Honor `wakeup_seconds` from the contract verbatim if set.
- Otherwise follow the harness's own ScheduleWakeup guidance: match the delay to what you are actually waiting for (a slow validator earns one long delay, not many short ones; tight iteration earns 60–120s). Do not try to pace around prompt-cache TTLs — cache behavior varies across harness versions and the harness's live guidance is authoritative.
- The runtime clamps to [60, 3600].

## Hard rules

The first three are mechanically enforced (PreToolUse hook + gk refusals) — treat a block or refusal as state information, never as an obstacle to route around.

- **Never modify `contract.md` mid-run.** If the spec is wrong, stop and ask the user to amend it explicitly via /goal-clear + new prep.
- **The log is append-only**, and all appends go through `gk checkpoint` / `gk verdict`.
- **State files are gk-owned.** No hand-written JSON.
- **Never skip the validator.** Never declare "done" without judge approval.
- **One execution loop per goal.** Do not spawn parallel iterations on the same slug.
- **Rejection counter only resets** on judge approval or an explicit user choice at `/goal-resume`.
- **Anti-placeholder:** do not stub, mock, or skip work to make the validator pass. The judge will catch placeholders and reject. (Borrowed from the Ralph pattern.)
