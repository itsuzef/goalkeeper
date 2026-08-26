<div align="center">
  <img src="branding/lockup.svg" alt="goalkeeper" width="360">
  <br>
  <h3>Set durable goals. Approve at the gate.</h3>
  <p>Contract-driven autonomous goal execution for <a href="https://docs.claude.com/en/docs/claude-code/overview">Claude Code</a>.<br>A subagent judge gates completion against an explicit Definition of Done.</p>
  <p>
    <a href="https://github.com/bonfire-systems/goalkeeper/actions/workflows/test.yml"><img alt="test" src="https://github.com/bonfire-systems/goalkeeper/actions/workflows/test.yml/badge.svg"></a>
    <img alt="license" src="https://img.shields.io/badge/license-MIT-0E7C66">
    <img alt="version" src="https://img.shields.io/badge/version-0.1.11-0E7C66">
    <img alt="claude-code" src="https://img.shields.io/badge/claude--code-plugin-0E7C66">
  </p>
</div>

<!--
DEMO SLOT — when you record an asciinema cast, replace this comment with:

<div align="center">
  <a href="https://asciinema.org/a/XXXXXX">
    <img src="https://asciinema.org/a/XXXXXX.svg" alt="goalkeeper reject-then-approve demo" width="720">
  </a>
</div>

Swap XXXXXX for the cast ID returned by `asciinema upload demo.cast`.
Until then, the example below is the canonical "real reject-cycle" reference.
-->

> **Real reject-cycle, 3 minutes wall-clock.** Goalkeeper caught a `MAX_RUNTIME_MS = 9999 // TODO` sentinel placeholder in a benchmark test where the validator passed both rounds. Round 1: validator green, judge **reject** on DoD #3 + #7 with a 3-step fix-list. Round 2: real threshold (500ms = 10x measured baseline) with justification, validator green, judge **approve**. Full transcript with quoted verdicts: [`docs/demo.md`](./docs/demo.md). Long-form writeup on [dev.to](https://dev.to/itsuzef/the-judge-gate-why-a-passing-validator-isnt-a-finished-feature-f2f).

---

## How it differs

|  | OpenAI Codex `/goal` | Ralph loop | **goalkeeper** |
|---|---|---|---|
| Durable goal across many turns | ✓ | ✓ | ✓ |
| Validator back-pressure | optional | core | core |
| **Independent judge gate against an explicit Definition of Done** | — | — | **core** |
| Anti-placeholder rule (stubs / `.todo` / `it.only` auto-reject) | — | informal | **enforced** |
| Linear chains of role-specific goals with judge-gated handoff | — | — | **`/goal-chain`** |
| Auto-pauses after N consecutive rejections | — | — | **5 (configurable)** |
| Append-only checkpoint log per goal | — | informal | enforced |
| Spec lives in | CLI prompt | `PROMPT.md` | `contract.md` (validated against JSON Schema) |
| Pre-existing validator failures distinguished from goal-caused | — | — | **`validator_baseline_*`** |

Inspired by [OpenAI Codex `/goal`](https://developers.openai.com/codex/use-cases/follow-goals) and Geoffrey Huntley's [Ralph loop](https://ghuntley.com/ralph/), with one key addition: the judge.

## Why

Codex `/goal` runs autonomously until a stop-condition is met. The Ralph loop runs `while :; do cat PROMPT.md | claude-code; done` and leans on validators (compile, test, lint) to back-pressure the model. Both work, but both have the same failure mode: **a passing validator is not the same as a finished feature**. Tests can pass on stubs. Linters can pass on `.todo`s. Codex can declare victory the moment its stop-condition string matches.

goalkeeper adds a second gate. After your validator passes, a fresh subagent — independent context, no rationalizations from the executing agent — reviews the diff and the progress log against your written Definition of Done, and either approves or returns a structured fix-list. After 5 rejections it pauses and asks for human help.

## Install

Add the goalkeeper marketplace, then install the plugin:

```bash
# inside Claude Code
/plugin marketplace add bonfire-systems/goalkeeper
/plugin install goalkeeper@goalkeeper
```

Skills become available under the `goalkeeper:` namespace. Invoke as `/goalkeeper:goal "<objective>"`, `/goalkeeper:goal-prep`, `/goalkeeper:goal-judge`, etc. (Most users alias the namespace away — see "Aliasing" below.)

### Aliasing the namespace

If you primarily use goalkeeper and want shorter commands, add aliases to `~/.claude/settings.json`:

```json
{
  "aliases": {
    "/goal": "/goalkeeper:goal",
    "/goal-prep": "/goalkeeper:goal-prep",
    "/goal-pause": "/goalkeeper:goal-pause",
    "/goal-resume": "/goalkeeper:goal-resume",
    "/goal-clear": "/goalkeeper:goal-clear",
    "/goal-judge": "/goalkeeper:goal-judge",
    "/goal-chain": "/goalkeeper:goal-chain"
  }
}
```

## Quick start

```
/goal-prep "Migrate the test suite from Jest to Vitest"
```

goalkeeper reads your repo, asks a few targeted questions (objective, definition-of-done, validator command, non-goals), writes `.claude/goals/<slug>/contract.md`, and offers to start. Once running, it:

1. Works in checkpoints (logged to `log.md`).
2. Runs your validator after each checkpoint.
3. When the validator passes, spawns a subagent **judge** to check Definition of Done.
4. Judge approves → done. Judge rejects → fix-list logged, work continues. After 5 rejections → paused for you.

Check status anytime:

```
/goal
```

## Commands

| Command | What it does |
|---|---|
| `/goal "<objective>"` | Start (or resume) a goal. Auto-routes to `/goal-prep` if no contract exists yet. |
| `/goal` | Show status of the active goal: last checkpoint, validator state, rejection count. |
| `/goal-prep "<rough idea>"` | Interactively draft a contract — the highest-leverage step. Surveys the repo and uses targeted questions to lock in a precise spec. |
| `/goal-pause` | Pause without losing state. Pending wakeups become no-ops. |
| `/goal-resume` | Resume a paused goal. If resuming from `needs_human`, asks whether to reset the rejection counter. |
| `/goal-clear` | Stop and archive the goal to `.claude/goals/_archive/`. Files are moved, never deleted. |
| `/goal-judge` | Run the judge advisorily on the active goal (no state change). Useful as a manual sanity check. |
| `/goal-chain "<file>"` | Run a linear sequence of goals; the judge gates progression between them. |

## Contract format

A contract is a markdown file at `.claude/goals/<slug>/contract.md` with frontmatter:

```yaml
---
slug: jest-to-vitest-migration
objective: Migrate the test suite from Jest to Vitest with no behavioral regressions and a measurable speed improvement.
non_goals:
  - Do not rewrite test logic
  - Do not change source code under src/
definition_of_done:
  - All test files import from "vitest" instead of "@jest/globals"
  - jest.config.* is removed; vitest.config.ts exists with equivalent coverage thresholds
  - "pnpm test" runs the full suite under Vitest with 100% of previously-passing tests still passing
  - Wall-clock test runtime improves by at least 20% vs the Jest baseline
validator:
  command: pnpm test --run && pnpm exec node scripts/check-no-jest-refs.mjs
  success: exit_zero
  timeout_seconds: 1200
checkpoint_cadence: every 5 file edits OR every 20 minutes
max_rejections: 5
judge_mode: subagent
wakeup_seconds: 270
---

## Context
<freeform body — file pointers, constraints, hints, anti-placeholder reminders>
```

The body of the contract is your "PROMPT.md" — file pointers, constraints, hints. See [`examples/`](./examples/) for full contracts.

Schema: [`schemas/contract.schema.json`](./schemas/contract.schema.json).

### Field reference

- **slug** — kebab-case directory name. Must be unique within `.claude/goals/`.
- **objective** — one sentence, what success looks like.
- **non_goals** — explicit out-of-scope items. Judge rejects on violation even if DoD is met.
- **definition_of_done** — what the judge grades against. Be specific; "works correctly" is not a DoD.
- **validator.command** — runs after each checkpoint. Necessary but not sufficient.
- **validator.success** — `exit_zero` (default) or `regex:<pattern>` matched against stdout.
- **checkpoint_cadence** — agent's guidance for how often to log + validate.
- **max_rejections** — default 5. After this many judge rejections, pauses for human.
- **judge_mode** — `subagent` (default, gate-quality) or `inline` (cheap, advisory only).
- **wakeup_seconds** — between-iteration delay. Tune to validator runtime; if unset, the model picks per the harness's ScheduleWakeup guidance.
- **diff_excludes** — additional pathspec globs the judge should ignore. Appended to defaults (lockfiles, `dist/`, `build/`, `coverage/`, IDE files). Add per-repo noise like generated migrations or vendor trees.

## Chains

A chain is an ordered list of slugs. The judge gates progression — only after approval does the next goal start.

```markdown
---
name: bonfire-bass-rust-port
---

1. port-dsp-core-to-rust
2. wire-up-ffi-shim
3. swap-cpp-for-rust-in-host
4. delete-cpp-tree
```

Run with:

```
/goal-chain ".claude/goals/chains/bonfire-bass.md"
```

Each slug must already have a contract at `.claude/goals/<slug>/contract.md`. Run `/goal-prep` for each before starting the chain.

## Multi-role goals

Most non-trivial goals span multiple roles — backend changes, UI wiring, migrations, tests, docs. Goalkeeper deliberately keeps just two agent slots (executor + judge) and stays framework-agnostic about how you spawn specialists. There are two patterns for getting role-shaped work through goalkeeper, and the right choice depends on how decoupled the roles are.

### Pattern A — chain of role-specific contracts (default)

Frame each role as its own contract with its own Definition of Done, validator, and judge. Compose them with `/goal-chain`. The judge gates progression: backend's judge has to approve before UI starts; UI's judge has to approve before migrations.

```markdown
---
name: stripe-integration
---

1. stripe-api-routes
2. stripe-db-migration
3. stripe-checkout-ui
4. stripe-e2e-tests
```

Each slug has its own `.claude/goals/<slug>/contract.md` with a focused DoD ("Stripe webhook signature verification works", "checkout component handles 3DS challenge", etc.) and a focused validator (`pnpm test packages/api`, `pnpm test packages/web`, etc.). See [`examples/chain.md`](./examples/chain.md) for a worked example.

**Choose Pattern A when:** roles are sequenceable. One role's work produces the artifacts the next role consumes. The dependency graph is linear or close to it. You want the judge to gate-keep at each role boundary so a sloppy backend can't silently corrupt the UI step.

**Tradeoff:** chains force upfront decomposition. You have to know the boundaries before you start. If the boundaries shift mid-flight, you have to clear the chain and re-plan.

### Pattern B — in-goal `specialists:` orchestration (proposed for v0.2)

For tightly-coupled cross-role work — same files, can't be sequenced — Pattern A creates artificial mid-flight pauses. Pattern B is the escape hatch: a single contract with a `specialists:` field that lists the roles available, plus role-specific system prompts. The executing agent is the orchestrator and dispatches subtasks to specialist subagents (Claude Code general-purpose agents with custom system prompts, your own `subagent_type` definitions, or MCP-based agents — goalkeeper doesn't ship specialist definitions).

```yaml
specialists:
  - role: backend
    system_prompt: "You are a senior Node.js engineer. ..."
  - role: ui
    system_prompt: "You are a senior React engineer. ..."
```

The judge still gates final approval against the unified DoD; the executing agent's log records which specialist did what.

**Status: proposed for v0.2 — not yet implemented.** The `specialists:` field is not in `schemas/contract.schema.json` yet. If you need this today, write your own orchestration in the body of the contract and have the executing agent spawn subagents using whatever Claude Code mechanism you prefer; goalkeeper observes via the log and the judge gates the result against the unified DoD.

**Choose Pattern B when:** specialists genuinely have to interleave on the same files. A frontend change requires a simultaneous backend change, both committed together, both tested together. Pattern A would create a contract that artificially pauses the work mid-flight.

**Tradeoff:** the executing agent owns coordination state, and the judge has to reason about multiple specialists' contributions in a single DoD pass. Easier bugs to hide than in chains.

### Default: prefer A unless you can articulate why A doesn't fit

If you can list the roles in dependency order and each role can complete independently before the next starts, use Pattern A. Reach for Pattern B only when the roles must touch the same code in the same edit.

## Missions — supervised iterative arcs (v0.2)

For multi-goal work where each next goal's shape is informed by the prior goal's actual output, the supervisor primitive sits one level above goals. Where the judge gates a *goal* against its Definition of Done, the supervisor gates the *mission* against its charter and adaptively decides what goal to run next.

Authoring a mission charter at `.claude/mission.md`:

```markdown
# Mission: v2-cutover

## Objective
Ship V2 cold-inbound to production safely.

## Success condition
All 6 cutover gates pass on a 2-week shadow window + 5%→25%→100% canary clean.

## Constraints
- Never send V2 outbound during shadow.
- Any cutover gate fail → fix + restart shadow.
- Max 2 weeks elapsed before user checkpoint.

## Legal next-goal shapes
- shadow-infra — assemble pipeline + tee + writer
- gate-fix — patch a specific failing axis from shadow data
- canary-rollout — % hash routing
- rollback — revert canary % to 0

## Done is not
- Just "validator passes." Done is "6 gates pass on shadow + canary green at 100%."
```

After a goal completes, `/goalkeeper:goal-supervisor` reads the charter + the prior goal's artifacts and decides:

- **PROCEED** — drafts the next goal's objective (informed by what the prior goal actually produced) and hands off to `/goalkeeper:goal-prep` for user-reviewable contract drafting.
- **DONE** — mission's success condition is satisfied; writes `.claude/mission-completed.md` with evidence per success-condition item.
- **ESCALATE** — supervisor can't decide; surfaces the ambiguity to the user with concrete required input.

Missions are NOT chains. Chains commit to a pre-determined linear sequence; missions adaptively decide the next goal based on prior outputs. Pick the right primitive: chains for sequenceable work known up front, missions for arcs where the shape of step N+1 depends on what step N actually produced.

## State and storage

```
.claude/
  mission.md                     # user-authored charter (v0.2, opt-in)
  mission.json                   # supervisor state
  mission-log.md                 # append-only mission-level audit trail
  mission-completed.md           # final snapshot on supervisor DONE
  goals/
    active.json                  # pointer to current slug (or terminal record)
    chain.json                   # current chain, if any
    .gitignore                   # ignores all goal dirs except shared/ (opt-in)
    <slug>/
      contract.md                # the spec
      state.json                 # status, rejection_count, git baseline, validator/judge results
      log.md                     # append-only checkpoint + verdict log
    _archive/                    # cleared goals (moved, never deleted)
    shared/                      # opt-in committed contracts for team sharing
```

By default everything under `.claude/goals/` is gitignored. To share a contract with your team, place it under `.claude/goals/shared/<slug>/contract.md`. The mission files (`mission.md`/`mission.json`/`mission-log.md`) live at `.claude/` root and are NOT auto-gitignored — author your mission charter the way you want it tracked.

### The gk CLI — mechanism lives in code, not prose (v0.4)

Every state transition — activate, checkpoint, validate, judge verdict, chain advance, pause, resume, clear, and (v0.5) mission init/verdicts — is executed by [`scripts/gk.py`](./scripts/gk.py), a dependency-free Python CLI. The skills carry judgment (what to do next, what to ask the user, what to put in a prompt); gk carries mechanism (every write to the files below). This is what makes the shapes drift-proof: there is exactly one implementation, exercised end-to-end by [`scripts/test-gk.py`](./scripts/test-gk.py).

Two consequences worth knowing as a user:

- **The hard rules are enforced, not requested.** A PreToolUse hook ([`hooks/hooks.json`](./hooks/hooks.json)) blocks the model from directly editing an active goal's `contract.md`, `log.md`, or `state.json` — contract immutability and the append-only audit trail are mechanical guarantees. The hook fails open: with no active goal it allows everything.
- **Recovery is a command.** If a session dies mid-chain-advance, `gk doctor --fix` detects and repairs the inconsistency (stale pointers, missing state, un-advanced cursors). You can also run `gk status` yourself in any terminal for an instant read on the active goal.

### Canonical state shapes

All skills read and write the same shapes via gk. Single source of truth is `scripts/gk.py` (asserted by both test suites); the reference here is for users.

**`active.json`** — exactly two shapes, active or terminal:

```json
// Active
{"slug": "<slug>", "activated_at": "<ISO>", "chain": "<chain>"}  // chain is optional

// Terminal
{
  "slug": null,
  "ended_at": "<ISO>",
  "ended_reason": "done" | "cleared" | "chain_completed" | "aborted",
  "previous_slug": "<slug>",
  "previous_chain": "<chain>"
}
```

**`<slug>/state.json`** — required fields on activation: `status`, `rejection_count`, `started_at`, `started_at_commit`, `started_at_dirty_paths`. Optional baseline-capture fields: `validator_baseline_result` (pass/fail/not_runnable/null) and `validator_baseline_failing_paths` (paths the validator was already failing on at activation — the judge subtracts these so pre-existing debt doesn't block goal approval). Populated as the goal runs: `chain_step`, `last_checkpoint_at`, `last_validator_result`, `last_judge_verdict`, `approved_at`, `paused_at`, `resumed_at`, `needs_human_at`.

**`chain.json`** — `name`, `slugs[]`, `cursor`, `status`, `started_at`, `completed_at`, `source_file`, `link_approvals[]`. The `link_approvals` array accumulates `{slug, approved_at}` entries as each link is judge-approved, providing chain-level visibility independent of per-link state files.

## Design notes

- **The contract is the spec.** Bad contracts produce bad work. `/goal-prep` is mandatory because thin contracts are goalkeeper's #1 failure mode.
- **Judge ≠ validator.** Validators check that things work; the judge checks that the *right* things work. Validator passing is necessary but not sufficient.
- **Subagent judge is the gate.** Independent context catches the placeholders and shortcuts the executing agent rationalized away. Inline judge mode exists for cheap advisory review only — do not use it as a gate.
- **Append-only log.** Logs are forensic artifacts. Past entries are never deleted or rewritten.
- **Skills decide, gk writes.** (v0.4) Prose-as-runtime was goalkeeper's original architecture and its biggest liability: every invariant depended on the model faithfully hand-executing a state machine described across five skill files. Moving the mechanics into one tested CLI makes verdicts reproducible (judge briefs are assembled byte-for-byte identically), makes interruptions recoverable (`gk doctor`), and turns the hard rules into hook-enforced guarantees.
- **Wakeup pacing defers to the harness.** Set `wakeup_seconds` in the contract to pin a delay; otherwise the model follows the harness's live ScheduleWakeup guidance rather than goalkeeper second-guessing prompt-cache behavior that changes across versions.
- **Anti-placeholder.** Borrowed verbatim from Ralph: stubs, mocks, `.todo`, `.skip`, and "TODO: real implementation" are automatic judge rejection.

## Prior art

- **OpenAI Codex `/goal`** — the durable-objective + stop-condition pattern. goalkeeper mirrors the lifecycle (set, status, pause, resume, clear) and adds prep, judge, and chain. ([docs](https://developers.openai.com/codex/use-cases/follow-goals))
- **Ralph (Geoffrey Huntley)** — the embedded-validator loop. goalkeeper adopts the validator philosophy and the anti-placeholder rule, and adds an external judge. ([blog post](https://ghuntley.com/ralph/))

## License

MIT — see [LICENSE](./LICENSE).

## Brand

Brand assets (logo, wordmark, social card) live in [`branding/`](./branding/) — see the [brand README](./branding/README.md) for usage notes, color palette, and SVG→PNG conversion. Drafts and alternates are intentionally outside this repo.

---

<div align="center">
  <img src="branding/mark.svg" alt="goalkeeper" width="48">
</div>
