---
name: goal-supervisor
description: The mission-level supervisor. One level above goals. Reads the user's mission charter and the most-recently-completed goal's artifacts, then decides whether to PROCEED (draft the next goal's contract for user review — never auto-activate), declare the mission DONE, or ESCALATE to the user. Use this skill when the user invokes /goal-supervisor after a goal completes, or when running a multi-goal arc where each next goal's shape is informed by the prior goal's actual output.
---

You are operating the **goal-supervisor** skill — the mission-level layer sitting one above individual goals. Where the judge gates a *goal* against its DoD, the supervisor gates the *mission* against its charter and decides what goal to run next.

The gk CLI (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gk.py"`, abbreviated `gk`) owns all mission mechanics: initialization, brief assembly, verdict application, and every write to `mission.json` / `mission-log.md` / `mission-completed.md` (a PreToolUse hook blocks direct edits to those while the mission is live; the user's charter `mission.md` is never blocked). Your job: spawn the supervisor subagent and relay its verdict faithfully.

## Mental model

```
Mission                                         ← .claude/mission.md (user-authored charter)
  ├─ Goal A   (done, in _archive)               ← goalkeeper contract — within-goal loop works
  ├─ Goal B   (done, in _archive)               ← drafted in response to A's output
  └─ Goal …   (drafted on demand by supervisor) ← what /goal-supervisor produces
```

The supervisor is **NOT** a chain. Chains commit to a linear sequence at chain-start. The supervisor decides direction adaptively based on what the prior goal actually produced.

## When to invoke

- After a standalone goal completes (`gk status` reports "No active goal").
- When a multi-goal mission is in flight and you want the next goal to be informed by the prior one.
- NEVER while a goal is in flight — gk refuses (active, paused, and needs_human all count as in-flight).

## Mission charter (`mission.md`) — expected shape

The charter is user-authored — the skill never auto-drafts missions. Sections (none syntactically required, all strongly recommended):

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

### Step 1 — initialize (idempotent)

```
gk mission-init
```

gk refuses without a charter at `.claude/mission.md`, refuses while a goal is in flight, initializes `mission.json` + the mission log on first run (creating `.claude/goals/` if this is the project's first goalkeeper use), and no-ops if the mission already exists. Relay any refusal to the user verbatim.

**If `gk mission-status` shows the mission is `escalated`** and the user is re-invoking the supervisor after addressing the required input, run

```
gk mission-resume --note "<how the user resolved the escalation>"
```

first — it flips the mission back to active and logs the resolution; without it, gk refuses further verdicts. Re-invoking `/goal-supervisor` after an escalation is the user's signal that they resolved it, but if their message doesn't say how, ask before resuming rather than inventing a resolution note.

### Step 2 — assemble the brief

```
gk mission-brief
```

This emits the complete supervisor prompt: charter verbatim, mission progress (goals completed + prior verdicts), the prior goal's `state.json` and compacted log (gk locates the most-recently-ended goal itself — live or archived; on a brand-new mission it frames the "first invocation" case), repo state, and the PROCEED/DONE/ESCALATE task block with the verdict format and escalate-over-proceed failure modes baked in. Do not edit the brief — comparable verdicts require identical prompt shape.

### Step 3 — spawn the supervisor subagent

Use the Agent tool with `subagent_type: general-purpose` and the brief as its entire prompt. Fresh context is mandatory — the supervisor must not inherit the executing agent's reasoning.

### Step 4 — apply the verdict

Pipe the subagent's **complete structured response** (VERDICT / REASONING / NEXT_OBJECTIVE / DONE_EVIDENCE / ESCALATION, verbatim) into gk:

```
gk mission-verdict proceed|done|escalate   <<'EOF' ... EOF
```

gk appends the mission-log entry, records the verdict and the prior goal's completion (result, rejection count), notes the verdict in the prior goal's own log, updates mission status, and on `done` writes the `mission-completed.md` snapshot. It also enforces one-invocation-per-goal-completion — a second verdict against the same prior goal is refused.

Then act on what it printed:

- **`PROCEED`** — hand the printed NEXT_OBJECTIVE to `/goalkeeper:goal-prep` as the rough idea. The user reviews and approves/edits the drafted contract per the standard prep flow. **Do NOT auto-activate** — the user-review checkpoint at prep is the human-in-the-loop safety property and is mandatory. "PROCEED" means "propose and draft", never "activate". Tell the user: "Supervisor verdict: PROCEED. Drafting next goal: `<objective>`. Review the contract before activating."
- **`DONE`** — tell the user: "Supervisor verdict: DONE. Mission `<name>` complete. See `.claude/mission-completed.md` for the final snapshot."
- **`ESCALATE`** — tell the user: "Supervisor cannot decide. Required input: <printed escalation, verbatim>. Resolve the question and run `/goalkeeper:goal-supervisor` again (it resumes the mission via `gk mission-resume` and re-runs the verdict against the same prior goal), or `/goalkeeper:goal-prep` a specific next goal yourself."

## Hard rules

- **Mission charter is required and user-authored.** No `mission.md` → gk halts. The skill does not auto-draft missions; that's user intent.
- **Supervisor never modifies a running goal.** gk refuses while any goal is in flight.
- **One supervisor invocation per goal-completion** — gk enforces it; on refusal, report the already-recorded verdict instead of retrying.
- **On PROCEED, the drafted contract requires user approval before activation.** Do NOT bypass.
- **Mission state is gk-owned and append-only.** `mission.json` / `mission-log.md` / `mission-completed.md` are hook-protected while the mission is live; the charter stays user-editable. If a prior verdict was wrong, the user can `/goalkeeper:goal-clear` the drafted-but-unactivated goal and re-run the supervisor — both verdicts stay in the log.
- **The supervisor is not a chain.** Chains are pre-committed linear sequences with judge gates; missions are adaptive arcs where each next goal is drafted from prior outputs.

## When NOT to use the supervisor

- For a single goal you can prep + activate directly — the supervisor's overhead is wasted on a one-off.
- For a pre-committed linear sequence designed up front — use `/goalkeeper:goal-chain`.
- For "I'm not sure what to build next on this project broadly." The supervisor needs a concrete mission; if you can't write a `Success condition` section, it isn't the right tool yet.
