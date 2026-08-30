---
name: goal-chain
description: Run a linear sequence of goalkeeper goals where the judge gates progression between them. Use when the user invokes /goal-chain "<file>" to start a chain. Chain bookkeeping (cursor, approvals, activation) is executed by the gk CLI.
---

You are operating the **goal-chain** skill. A chain is a linear ordered list of goal slugs that execute one after another, gated by judge approval at each step.

The gk CLI (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gk.py"`, abbreviated `gk`) owns all chain mechanics: parsing and validating the chain file, cursor movement, link approvals, and next-goal activation. You orchestrate: spawn executor → spawn judge → let `gk verdict` apply the outcome → act on what it prints.

## Modes

1. **Start mode** — `args` is a non-empty path to a chain file. Begin a new chain.
2. **Status mode** — `args == "status"` or a chain exists and the user asks plainly. Show progress.

(There is no separate "advance mode" anymore — `gk verdict <slug> approve` advances the cursor atomically as part of applying the verdict.)

## Start mode

Triggered by `/goal-chain "<path/to/chain.md>"`.

### 1. Start the chain

```
gk chain-start <path/to/chain.md>
```

gk parses the file (frontmatter `name:` plus a numbered or bulleted slug list, `#` comments stripped), verifies every slug has a `contract.md` (a missing contract aborts start — prep all contracts up front so the chain definition is reviewable), refuses to start over an active goal or chain, writes `chain.json`, and activates the first slug. It prints `NEXT: <slug>` on success; on refusal, relay its message to the user (usually: run `/goal-prep` for missing slugs, or `/goal-clear` first).

### 2. Spawn the executor subagent

Chains run per-goal implementation in a **fresh-context subagent** — main context only orchestrates. This keeps main-context cost flat (~10K tokens per goal) so a many-goal chain completes in one session.

Use the Agent tool with `subagent_type: general-purpose`. Assemble a self-contained prompt:

1. **The full `contract.md`** — verbatim.
2. **The compacted log** — output of `gk log <slug> --compact` (activation entry, every judge block, recent checkpoints — includes any fix-list if this is a re-spawn after rejection).
3. **Chain context** — chain name, current step N of total, the prior link's approval timestamp.
4. **Repo state** — `git rev-parse HEAD`, `git status --porcelain` (first 20 lines).
5. **The directive** — the template below, with `<GK>` replaced by the **resolved absolute** gk command (e.g. `python3 /path/to/goalkeeper/scripts/gk.py`) — the subagent does not inherit `${CLAUDE_PLUGIN_ROOT}`:

```
You are the goalkeeper EXECUTOR SUBAGENT for goal `<slug>` (chain step <N>/<total>).

Your job: read the contract above, execute every Definition-of-Done item, run
the validator at the end, and return a structured summary. You operate in a
fresh context — you have no conversation history beyond this prompt. The
contract is your spec; do not improvise outside it.

Goalkeeper state mechanics run through the gk CLI: `<GK>`. Never edit the
goal's log.md, state.json, or contract.md directly — a hook blocks it.

Execution loop:
  1. Read the contract carefully. Identify the implementation work required.
  2. Do the work. Edit/write project files as needed. Follow the contract's
     non-goals and anti-placeholder rule strictly.
  3. Checkpoint as you go: `<GK> checkpoint <slug> --message "<what changed,
     files touched, decisions>"` — one per logical sub-task or every ~5 file
     edits.
  4. Run the validator: `<GK> validate <slug>` (exit 0 = pass, 1 = fail; it
     updates state and prints the output tail).
  5. If it FAILS: diagnose, fix, re-run. Repeat up to ~3-5 inner attempts.
     If still failing, checkpoint why and return with STATUS: validator_fail.
  6. If it PASSES: return.

DO NOT spawn the judge yourself. DO NOT run gk verdict, gk advance, gk
chain-start, or gk clear. DO NOT activate the next goal. Just do the work
for THIS goal and return.

Return ONCE with this structured output:

STATUS: validator_pass | validator_fail | blocked | needs_clarification

SUMMARY:
<3-8 sentences describing what you did: files touched, decisions made, any
non-obvious tradeoffs, anything the judge should pay particular attention to>

VALIDATOR_OUTPUT_TAIL:
<last ~40 lines of the validator's stdout+stderr>

FILES_CHANGED:
<bulleted list of paths modified relative to repo root, plus a one-line
note per file about what changed>

BLOCKERS: (only if status != validator_pass)
<specific reason: which DoD item, which file, what's missing or wrong>
```

### 3. Receive executor return, spawn judge

- **STATUS = blocked or needs_clarification** — checkpoint the BLOCKERS verbatim (`gk checkpoint <slug> --message "EXECUTOR BLOCKED: <blockers>"`), then split on the blocker's kind:
  - **Human-gated** (a credential, consent, account access, a decision only the user can make): `gk park <slug> --needs "<exactly what a person must provide>"`. This frees the active slot and sets the chain to `waiting` — **the goal is parked, not you.** Tell the user in one line what is needed, then **continue with other work** (another goal, another repo, the rest of your portfolio). When the user unblocks it, `gk resume <slug>` re-arms the chain.
  - **Solvable** (missing tool, unclear code, failing environment): that is work, not a blocker — re-spawn the executor with the clarification added to the prompt. Do not park what an agent can fix.

  Do NOT invoke the judge on a blocked return.

- **STATUS = validator_fail** — a test/lint failure is always the solvable kind: re-spawn the executor with the failure named. Never park on a validator failure.

- **STATUS = validator_pass** — write the executor's structured return to a scratch file and invoke the **goal-judge** skill (subagent mode) — it runs `gk judge-brief <slug> --executor-summary <file>`, spawns the judge, and applies the verdict with `gk verdict`.

### 4. Act on what gk verdict printed

- **`NEXT: <slug>`** — the link was approved; gk marked it done, recorded the approval, advanced the cursor, and activated the next goal (with a fresh git baseline so the previous link's output counts as pre-existing dirt for the next judge). Spawn the executor subagent for the printed slug per step 2, then loop back here.
- **`CHAIN_COMPLETE`** — gk closed the chain and wrote terminal state. Tell the user: "Chain `<name>` complete. <N> goals approved sequentially — per-link timestamps in `chain.json.link_approvals`."
- **`RETRY`** — judge rejected within budget; the fix-list is in the log. **Re-spawn the executor subagent** with the same prompt structure (the compacted log now carries the fix-list) plus one added directive line: "Address the judge's fix-list from the most recent 'judge rejected' block, then proceed per the standard execution loop." Loop back to step 3.
- **`NEEDS_HUMAN`** — max rejections. gk already parked the goal (slot freed, chain `waiting`, the need recorded in state). Do not advance. Surface the fix-list verbatim, point at `gk resume <slug> --reset-rejections` / `/goal-clear` — then **continue with other portfolio work.** A parked goal never idles the agent.

## Recovery

If a chain ever looks inconsistent (crash mid-advance, interrupted session):

```
gk doctor          # report inconsistencies
gk doctor --fix    # repair them
```

Doctor detects and repairs the known failure shapes: stale `active.json`, missing next-link state, approved-but-not-advanced cursor, and missing `link_approvals` entries. `gk advance --fix` exists as a manual last resort. Never hand-edit chain state to "fix" it.

## Status mode

Run `gk status` for the summary line, then read `chain.json` and per-link `state.json` files to print:

```
Chain:     <name>
Source:    <source_file>
Status:    <active|waiting|done|aborted>
Progress:  <cursor>/<N>
Goals:
  [x] <slug 1>  — done       approved <ISO8601 from link_approvals>
  [>] <slug 2>  — active     rejections: <n>/<max>
  [ ] <slug 3>
```

For a `waiting` chain, also print the parked goal's `needs` line from `gk status` — that is the human's unblock instruction.

Plain ASCII markers `[x]` / `[>]` / `[ ]`. No emoji.

## Interaction with goal-clear

`/goal-clear` during an active chain aborts the chain (`gk clear --yes` sets `chain.json.status = aborted`) and does NOT advance. Clearing means "stop everything." Unreached links' contracts stay in place for re-use.

## Hard rules

- **One chain at a time.** No nested or parallel chains. (Parallel lanes belong in worktrees.)
- **A parked goal parks the goal, never the agent.** A human-gated blocker frees the slot via `gk park`; the agent moves to other work and a person unblocks at their own pace.
- **Cursor only advances on judge approve** — and only via `gk verdict` / `gk advance`.
- **Don't archive chain goals on completion.** They form a traceable history; the user can `/goal-clear` later.
- **A missing contract aborts chain start.** Do not auto-prep mid-chain.
- **The orchestrator never does per-goal implementation work itself.** Spawn executor, spawn judge, act on gk's output — nothing else.
