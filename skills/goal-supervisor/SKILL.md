---
name: goal-supervisor
description: The mission-level supervisor. One level above goals. Reads the user's mission charter and the most-recently-completed goal's artifacts, then decides whether to PROCEED (draft the next goal's contract for user review — never auto-activate), declare the mission DONE, or ESCALATE to the user. Use this skill when the user invokes /goal-supervisor after a goal completes, or when running a multi-goal arc where each next goal's shape is informed by the prior goal's actual output.
---

You are operating the **goal-supervisor** skill — the mission-level layer sitting one above individual goals. Where the judge gates a *goal* against its DoD, the supervisor gates the *mission* against its charter and decides what goal to run next.

## Mental model

```
Mission                                         ← .claude/mission.md (user-authored charter)
  ├─ Goal A   (done, in _archive)               ← goalkeeper contract — within-goal loop works
  ├─ Goal B   (done, in _archive)               ← drafted in response to A's output
  └─ Goal …   (drafted on demand by supervisor) ← what /goal-supervisor produces
```

The supervisor is **NOT** a chain. Chains commit to a linear sequence at chain-start. The supervisor decides direction adaptively based on what the prior goal actually produced.

## When to invoke

- After a standalone goal completes (status `done` or `cleared` — `.claude/goals/active.json` in terminal shape; `gk status` reports "No active goal").
- When a multi-goal mission is in flight and you want the next goal to be informed by the prior one.
- NEVER while a goal is `active` — supervisor refuses if a goal is still running.

## Inputs

- `.claude/mission.md` — the user-authored mission charter. **Required** — supervisor refuses without it.
- `.claude/goals/active.json` — must be terminal-shape (no active goal). Check via `gk status` (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gk.py" status`).
- `.claude/mission.json` — supervisor state. May not exist on first invocation; supervisor initializes it.
- `.claude/mission-log.md` — append-only mission-level audit trail. May not exist on first invocation.
- The most-recently-ended goal's artifacts:
  - `.claude/goals/_archive/<slug-with-timestamp>/log.md` (preferred, if cleared)
  - or `.claude/goals/<slug>/log.md` (if done but not yet archived)
  - and the same goal's `state.json` for verdict + rejection_count context

## Mission charter (`mission.md`) — expected shape

The supervisor expects users to author `mission.md` with these sections. None are syntactically required (no JSON Schema), but all are strongly recommended:

```markdown
# Mission: <name>

## Objective

<one-paragraph statement of the mission's high-level intent>

## Success condition

<concrete, observable condition for "mission done." Like a goal's definition_of_done
but at the mission level. Specific. Measurable.>

## Constraints

<bulleted list of hard rules. The supervisor will refuse to propose goals
that violate these.>

## Legal next-goal shapes

<bulleted catalog of the kinds of goals this mission may need. The supervisor
draws from this list when proposing next-objectives. Each entry: name +
1-sentence description.>

## Done is not

<bulleted list of things that look like progress but don't satisfy the
success condition — equivalent to a contract's non_goals at the mission level.>
```

## Flow

### Step 1 — pre-flight

1. Read `.claude/mission.md`. If missing, halt with: "Supervisor requires `.claude/mission.md`. See goal-supervisor skill docs for the expected shape." Do not proceed.
2. Run `gk status`. If it reports an active (or paused / needs_human) goal, halt with: "Supervisor refuses while a goal is in flight. Pause or complete the current goal first." Only a "No active goal" terminal state may proceed.
3. Read `.claude/mission.json` if it exists; otherwise initialize:
   ```json
   {
     "name": "<from mission.md heading>",
     "status": "active",
     "started_at": "<ISO8601 now>",
     "goals_completed": [],
     "supervisor_verdicts": []
   }
   ```

### Step 2 — locate the prior goal

The most-recently-ended goal is the supervisor's primary input. In order of preference:

1. If `.claude/goals/active.json` has a non-null `previous_slug`: that's the slug (`gk status` also names it in its "Last:" line).
2. Else look in `.claude/goals/_archive/` for the most-recently-archived directory.
3. Else look in `.claude/goals/` for a directory whose `state.json.status == "done"` and isn't yet archived.

If no prior goal exists (first supervisor invocation on a new mission), the supervisor treats this as "the mission just started, no prior context — propose the first goal from `mission.md`'s `Legal next-goal shapes` section."

### Step 3 — spawn the supervisor subagent

Use the Agent tool with `subagent_type: general-purpose`. Fresh context — supervisor must NOT inherit the executing agent's reasoning.

Pass a self-contained prompt with:

1. **The full `mission.md`** — verbatim.
2. **The prior goal's log, compacted** — output of `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gk.py" log <slug> --compact` (activation entry + every judge/lifecycle block + recent checkpoints; works for archived goals too).
3. **The prior goal's `state.json`** — verbatim. (Tells the supervisor whether the goal was approved, how many rejections, etc.)
4. **The current `mission.json.goals_completed`** — list of prior slug + brief result summary.
5. **A list of supervisor-relevant repo state**: `git rev-parse HEAD` at this moment, `git status --porcelain` (first 20 lines).
6. **The task** — verbatim, in this exact format:

```
You are the mission supervisor. The user's mission is described above.
One goal has just completed. Your job: decide what happens next.

You have three legal outputs:

PROCEED — the mission is still active and the next goal can be named.
  Output a one-sentence objective for the next goal. Reference what the
  prior goal produced and how it shapes this one. The objective will be
  fed to /goalkeeper:goal-prep, which drafts a full contract from it.

DONE — the mission's success condition is satisfied. Cite the specific
  evidence in the prior goal(s) that demonstrates each part of the
  success condition.

ESCALATE — you cannot decide. Either the prior goal's output is
  ambiguous, the mission charter is internally inconsistent, the
  success condition isn't observable from the artifacts, or you've
  hit a constraint that requires human judgment. Explain in 3-5
  sentences exactly what decision needs human input.

Output ONCE. Pre-think before writing. Do not self-correct mid-response.

Respond in this exact format:

VERDICT: proceed
or
VERDICT: done
or
VERDICT: escalate

REASONING:
<3-8 sentences explaining what the prior goal produced, what it tells
you about mission progress, and why this verdict>

NEXT_OBJECTIVE: (only if proceed — single sentence, will be passed to /goal-prep)

DONE_EVIDENCE: (only if done — bulleted list of mission success-condition
items, each with the specific prior-goal artifact that satisfies it)

ESCALATION: (only if escalate — exactly what human input is needed and why)
```

### Step 4 — apply the verdict

Read the subagent's structured response. Then:

#### PROCEED

1. Append to `.claude/mission-log.md`:
   ```
   ## <ISO8601> — supervisor verdict: proceed
   Prior goal: <prior-slug>
   Reasoning: <from REASONING block>
   Proposed next objective: <from NEXT_OBJECTIVE>
   ```
2. Append to `mission.json.supervisor_verdicts`:
   ```json
   {"at": "<ISO8601>", "prior_slug": "<prior-slug>", "verdict": "proceed", "next_objective": "<...>"}
   ```
3. Append to `mission.json.goals_completed`:
   ```json
   {"slug": "<prior-slug>", "result": "<approved|cleared|...>", "rejection_count": <n>}
   ```
4. **Hand off to `/goalkeeper:goal-prep`** with the proposed next-objective as the rough idea. The user reviews and approves/edits the drafted contract per the standard prep flow. **Do NOT auto-activate** — the user-review checkpoint at prep is the human-in-the-loop safety property and is mandatory. "PROCEED" means "propose and draft", never "activate".
5. Tell the user: "Supervisor verdict: PROCEED. Drafting next goal: `<objective>`. Review the contract before activating."

#### DONE

1. Append to `.claude/mission-log.md`:
   ```
   ## <ISO8601> — supervisor verdict: done
   Mission: <mission name>
   Reasoning: <from REASONING block>
   Evidence: <from DONE_EVIDENCE block>
   ```
2. Update `mission.json`:
   ```json
   {
     "status": "done",
     "completed_at": "<ISO8601>",
     "goals_completed": [...append final...]
   }
   ```
3. Write `.claude/mission-completed.md` — a final snapshot with the verdict text, every prior goal's slug + result, total elapsed time, and a copy of the original `mission.md` for posterity.
4. Tell the user: "Supervisor verdict: DONE. Mission `<name>` complete. <N> goals approved. See `.claude/mission-completed.md` for the final snapshot."

#### ESCALATE

1. Append to `.claude/mission-log.md`:
   ```
   ## <ISO8601> — supervisor verdict: escalate
   Prior goal: <prior-slug>
   Reasoning: <from REASONING block>
   Required input: <from ESCALATION block>
   ```
2. Update `mission.json.status = "escalated"`.
3. Tell the user: "Supervisor cannot decide. Required input: <ESCALATION block, verbatim>. Resolve the question and run `/goalkeeper:goal-supervisor` again (it will re-read the latest artifacts), or `/goalkeeper:goal-prep` a specific next goal yourself."

### Step 5 — record the supervisor verdict on the prior goal

Append a brief line to the prior goal's `log.md` (since the goal's log is its archived audit trail and the supervisor verdict is relevant context for anyone reading later):

```
## <ISO8601> — supervisor verdict
Mission `<mission-name>` supervisor verdict on this goal: <proceed|done|escalate>.
See `.claude/mission-log.md` for full reasoning.
```

This makes the supervisor's verdict discoverable from inside the goal's own log without forcing readers to cross-reference the mission log.

## On-disk shapes

### `.claude/mission.json`

```json
{
  "name": "<from mission.md heading>",
  "status": "active" | "done" | "escalated",
  "started_at": "<ISO8601>",
  "completed_at": "<ISO8601 or absent>",
  "goals_completed": [
    {"slug": "<slug>", "result": "approved" | "cleared", "rejection_count": <n>, "ended_at": "<ISO8601>"}
  ],
  "supervisor_verdicts": [
    {"at": "<ISO8601>", "prior_slug": "<slug>", "verdict": "proceed" | "done" | "escalate", "next_objective": "<if proceed>", "escalation": "<if escalate>"}
  ]
}
```

### `.claude/mission-log.md`

Append-only, parallel structure to per-goal `log.md`. One entry per supervisor invocation. Sections delimited by `## <ISO8601> — supervisor verdict: <kind>`.

### `.claude/mission-completed.md`

Final snapshot written on DONE. Contains:
- Copy of the original `mission.md` (in case the user later edits the live one)
- Full `mission.json` at completion
- Bulleted list of all goals approved + their `log.md` paths in `_archive/`
- The final supervisor verdict's reasoning + evidence

## Hard rules

- **Mission charter is required.** No `mission.md` → supervisor halts. The skill does not auto-draft missions; that's user intent.
- **Supervisor never modifies a running goal.** Reads only. State writes are to mission-level files (`mission.json`, `mission-log.md`) plus one informational append to the prior goal's `log.md`.
- **One supervisor invocation per goal-completion.** If the user runs `/goal-supervisor` twice in a row without a new goal completing between, the second invocation is a no-op (return last verdict + tell user).
- **On PROCEED, the drafted contract requires user approval before activation.** This is the human-in-the-loop safety property. Do NOT bypass.
- **Append-only mission-log.** Never delete or rewrite past entries. If the supervisor's prior verdict was wrong, the user can `/goalkeeper:goal-clear` the drafted-but-unactivated goal and re-run `/goal-supervisor` — both verdicts stay in the log.
- **The supervisor is not a chain.** Don't confuse them. Chains are pre-committed linear sequences with judge gates. Missions are adaptive arcs where each next goal is drafted from prior outputs.

## When NOT to use the supervisor

- For a single goal you can prep + activate directly. The supervisor's overhead (charter + subagent verdict + draft + review) is wasted on a one-off.
- For a pre-committed linear sequence where you've already designed all goals up front. Use `/goalkeeper:goal-chain` instead — chains are the right primitive when the sequence is known.
- For "I'm not sure what to build next on this project broadly." Supervisor needs a concrete mission, not a vague hope. The charter forces specificity; if you can't write a `Success condition` section, the supervisor isn't the right tool yet.

## Failure modes worth flagging in the verdict

The supervisor subagent should ESCALATE rather than PROCEED in these cases:

- Prior goal was rejected the maximum number of times (`rejection_count == max_rejections`) and ended in `needs_human` — user already needs to address this manually before the mission continues.
- Prior goal touched files explicitly listed in the mission's `Constraints` as off-limits — mission charter integrity violated; user must decide whether to amend the charter or roll back.
- The success condition references a metric the prior goal didn't measure or produce evidence for. The supervisor can't verify done if the evidence isn't observable.
- The Legal next-goal shapes catalog doesn't contain a shape that fits what the prior goal's output suggests is needed next. (Often a signal that the charter is incomplete; the user should amend it.)
