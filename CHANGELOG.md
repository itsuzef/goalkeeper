# Changelog

All notable changes to **goalkeeper** are documented here.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.8.0] - 2026-08-30

`needs_human` now parks the goal, never the agent. Previously a goal waiting on a person held the single active slot hostage — the whole repo's goal work froze until someone showed up. Now a human-gated stop frees the slot, records exactly what a person must provide, and the agent moves on to other work; the human unblocks at their own pace.

### Added

- **`gk park [slug] --needs "<what a person must provide>"`.** Explicit entry point for a human-gated blocker (credential, consent, account access, a decision that is the user's to make): sets `needs_human`, stamps `needs`, frees `active.json`, and moves the goal's chain (if it heads one) to a new `waiting` status. Only an `active` or `paused` goal can be parked.
- **Parked queue in `gk status`.** Every `needs_human` goal is listed with its `needs` text and the `gk resume <slug>` that restarts it — with or without an active goal. This is the human's unblock queue.
- **`gk resume [slug]`.** Resume now takes an optional slug so parked goals (which hold no slot) can be resumed by name. It restores the slot, clears `needs`, and re-arms a `waiting` chain whose cursor points at the resumed goal. Refused while a different goal holds the slot — one loop at a time.

### Changed

- **Max rejections auto-parks.** `gk verdict reject` at the cap now frees the slot (previously the goal kept it in `needs_human`). The printed outcome is still `NEEDS_HUMAN`; the recorded `needs` points at the fix-list. The explicit `--reset-rejections`/`--keep-count` choice on resume is now required only when rejections are actually on the clock — a goal parked with a zero count resumes bare.
- **`gk chain-start` refuses over a `waiting` chain** as well as an active one (parallel lanes belong in worktrees, not overwritten chain files).
- **Hook-guard protects parked goals.** File protection now keys on the touched goal's own state (`active`/`paused`/`needs_human`) rather than only the slot holder, so a parked goal's contract, log, state, token, and receipt stay gk-owned after the slot is freed.
- **Skills updated** (goal, goal-chain, goal-pause, goal-resume): an executor's human-gated blocker is parked, not paused; a solvable blocker (missing tool, unclear code, validator failure) is work and is never parked; after any park the orchestrator continues with other portfolio work.

### Testing

- New end-to-end test (14 assertions): park frees the slot and records the need, other goals activate alongside, chain `waiting` blocks a second chain-start but survives an unrelated clear, status lists the queue, resume restores slot + chain and the resumed chain advances on approve. Updated the needs_human test to the parked semantics. 174 assertions all passing.

## [0.7.0] - 2026-08-29

A verdict now leaves the building: the CLI mints a self-contained, exportable receipt at the moment it consumes a judge token, so a consumer outside the goals directory — a TaskFlow cross-flow edge, a release gate, another repository's lane — can carry and re-verify what was decided without access to goal state.

### Added

- **Verdict receipts.** `gk verdict` on a provenance-v1 goal now writes `<slug>/receipt.json` alongside consuming the token: decision, executed judge mode, `gate_quality` (an approve delivered by the subagent judge — the one bit a cross-flow consumer keys on), the consumed token (id, minted_by, minted/used timestamps), executor observables from activation, the contract's SHA-256, and the repo commit judged (`head`, dirty paths, `started_at_commit`). Like the token, the receipt is written by the CLI at the moment of the verdict — never typed by a caller — and hook-guarded against direct edits. Rejections mint receipts too; they are never gate-quality.
- **`gk receipt <slug>`.** Prints the receipt for export. Refusals distinguish "no verdict accepted yet" from "pre-provenance goal" (goals activated before v0.6.0 mint no receipts and complete under their activation-time rules).

### Testing

- New end-to-end test (12 assertions): mint-on-reject and mint-on-approve, gate-quality bit, token/history agreement, contract-hash and repo-commit binding, hook-guard coverage of the receipt file, export via `gk receipt`, and the legacy path. 158 assertions all passing.

## [0.6.0] - 2026-08-27

Judge verdicts now carry provenance: which judge mode actually ran, enforced by a single-use token the CLI mints and consumes — an inline advisory read can no longer masquerade as a gate-quality subagent approval.

### Added

- **Judge verdict provenance.** `gk judge-brief` now mints a single-use judge token (`<slug>/judge-token.json`, hook-guarded) stamping the judge mode actually being run (`--mode` to declare inline; default is the contract's `judge_mode`). `gk verdict` consumes the token: a verdict with no unused token is refused, a consumed token cannot be reused, and caller-typed provenance is never accepted. An inline-minted token cannot deliver a gate-quality `approve` on a contract whose `judge_mode` is `subagent` — advisory verdicts do not convert into gate approvals.
- **Append-only verdict history.** Goal state now records every accepted verdict in `judge_verdicts` (`{at, verdict, mode, token_id, token_minted_at}`), mirroring the mission tier's `supervisor_verdicts` shape, alongside `last_judge_mode`. Previously the executed judge mode was never persisted anywhere — an approval was indistinguishable from an advisory inline self-read after the fact.
- **Executor observables at activation.** `state.json` records `provenance_version: 1` and best-effort caller identity (`user`, `GK_ACTOR`/`CLAUDE_SESSION_ID` when set) when a goal activates.

### Compatibility

- Goals activated before this release carry no `provenance_version` and complete under their activation-time rules — no token required, verdicts recorded with `legacy: true`. No in-flight goal is stranded by the upgrade.

### Testing

- New end-to-end test (18 assertions): mint/consume lifecycle, refusal without mint, single-use enforcement, inline→approve non-convertibility, hook-guard coverage of the token file, append-only history, and the legacy path. Existing verdict tests updated to mint first. 146 + 80 assertions all passing.

## [0.5.1] - 2026-08-26

Two mission-layer bugs (user-found), plus a third latent one behind the second:

### Fixed

- **Fresh mission initialization failed unless `.claude/goals/` already existed.** `gk mission-init` now calls `find_goals_dir(create=True)` — a mission legitimately starts before any goal has ever been activated, and init now seeds the goals dir (with its `.gitignore`) on a first-time project.
- **Escalated missions could not resume.** `mission-brief` worked after escalation, but every subsequent verdict was refused because status stayed `escalated`. New `gk mission-resume [--note TEXT]` performs the explicit escalated → active transition, logging the user's resolution; the supervisor skill runs it when re-invoked after an escalation.
- **The one-invocation-per-goal-completion guard also blocked the post-escalation re-run** (same prior goal → refused), which would have made `mission-resume` insufficient on its own. The guard now exempts a resolved escalation; un-resumed escalations are still blocked by the status gate, and the guard still refuses after a proceed/done.

### Testing

The original tests missed all three because fixtures pre-created `.claude/goals` and the escalation test only verified *entering* escalation. Added `test_mission_fresh_init` (no pre-existing goals dir) and rewrote the escalation test as a full recovery arc: escalate → verdict-refused → resume with note → same-prior-goal verdict accepted → guard still holds after proceed. 128 assertions total.

## [0.5.0] - 2026-08-26

**The mission layer gets the gk treatment.** The supervisor was the last tier whose state files were hand-written by prose; its mechanics now run through gk, its state files are hook-protected, and the layer finally has end-to-end tests.

### Added

- `gk mission-init` — requires the user-authored `.claude/mission.md` charter, refuses while any goal is in flight (active, paused, or needs_human), initializes `mission.json` + `mission-log.md`; idempotent on re-run.
- `gk mission-brief` — deterministic supervisor prompt assembly: charter verbatim, mission progress, the prior goal's `state.json` + compacted log (gk locates the most-recently-ended goal itself, live or archived, and frames the first-invocation case), repo state, and the PROCEED/DONE/ESCALATE task block with the escalate-over-proceed failure modes baked in.
- `gk mission-verdict proceed|done|escalate` — applies the supervisor's structured response from stdin: mission-log entry, verdict + prior-goal completion recorded in `mission.json`, a verdict note in the prior goal's own log, status transitions, and the `mission-completed.md` snapshot on done. Refuses a verdict missing its required section (NEXT_OBJECTIVE / DONE_EVIDENCE / ESCALATION) and enforces **one supervisor invocation per goal-completion**.
- `gk mission-status`, plus a mission summary line in `gk status`.
- Hook coverage: `mission.json`, `mission-log.md`, and `mission-completed.md` are blocked from direct Edit/Write while the mission is live (active or escalated). The charter `mission.md` is never blocked — it stays user-editable by design.
- 32 new assertions in `scripts/test-gk.py` (114 total): full mission lifecycle, escalate path, verdict-section requirements, double-invocation guard, and mission hook-guard block/allow behavior.

### Changed

- `skills/goal-supervisor/SKILL.md` rewritten to judgment-only: init → brief → spawn subagent → apply verdict via gk. The on-disk mission shapes moved out of the skill — `scripts/gk.py` owns them now, matching the goal/chain layer.

### Migration notes

- Mission file locations and shapes are unchanged (`.claude/mission.md` / `mission.json` / `mission-log.md` / `mission-completed.md`); an in-flight v0.2+ mission continues under v0.5.
- Hook changes require a plugin reload.

## [0.4.1] - 2026-08-26

Supervisor-layer fixes — two defects in `goal-supervisor/SKILL.md`:

### Fixed

- **Stale active-state path.** The pre-flight, inputs, and prior-goal-location steps referenced `.claude/active.json`; the file actually lives at `.claude/goals/active.json`. A supervisor following the instructions literally would read a nonexistent file, conclude no goal is active, and bypass its own refuse-while-active guard. State checks now route through `gk status` (which also treats paused/needs_human goals as in-flight, not just `active`).
- **Auto-activation contradiction.** The frontmatter description promised PROCEED would "draft + activate the next goal," while the body and hard rules mandate the opposite — the drafted contract goes through `/goal-prep`'s user-review checkpoint and is never auto-activated. The description now matches the body: PROCEED means "propose and draft," never "activate."

## [0.4.0] - 2026-08-26

**The gk CLI: state mechanics move from prose to code.** Every state transition (activate, checkpoint, validate, verdict, chain advance, pause, resume, clear) is now executed by `scripts/gk.py` — a single dependency-free Python CLI — instead of being hand-performed by the model following ~2,000 lines of SKILL.md instructions. Skills carry judgment (what to do); gk carries mechanism (the writes). A new PreToolUse hook turns goalkeeper's hard rules into mechanical guarantees.

### Why

v0.1–v0.3 documented a state machine in prose and trusted the model to execute it faithfully across five skill files that "MUST conform" to canonical shapes documented in a sixth place. Every invariant — append-only log, contract immutability, cursor-advance-on-approve — was a request, not a guarantee, and the chain skill needed a whole "Recovery from interrupted advance" section because interruptions could leave hand-written state inconsistent. Moving the mechanics into one tested script eliminates shape drift by construction, makes judge briefs byte-for-byte reproducible, and cuts the tokens previously spent re-deriving mechanical procedures every invocation.

### Added

- `scripts/gk.py` — the goalkeeper state mechanic. Subcommands: `status`, `activate`, `baseline`, `checkpoint`, `validate`, `judge-brief`, `verdict`, `chain-start`, `advance`, `pause`, `resume`, `clear`, `log`, `doctor`, `hook-guard`. Stdlib-only, Python 3.9+. Atomic JSON writes (tmp + rename). Real UTC timestamps instead of model-written ones.
- `hooks/hooks.json` — PreToolUse hook (Edit|Write|NotebookEdit) running `gk hook-guard`: blocks direct edits to the active goal's `contract.md`, `log.md`, `state.json`, and to `active.json` / `chain.json`, with a message pointing at the gk commands. Fails open when no goal is active, for `_archive/` and `shared/` paths, and on any parse error — it can never brick unrelated edits. Contract immutability and the append-only log are now enforced, not requested.
- `gk verdict <slug> approve|reject` applies a judge's structured response from stdin: extracts REASONS/FIX_LIST verbatim into the log, handles the rejection threshold, and — when a chain is active — records the link approval and advances the cursor **atomically** (prints `DONE` / `NEXT: <slug>` / `CHAIN_COMPLETE` / `RETRY` / `NEEDS_HUMAN` for the orchestrating skill).
- `gk judge-brief <slug> [--executor-summary <file>]` — deterministic judge-prompt assembly: contract verbatim, compacted log, baseline/dirty-path subtraction, deduped file list, exclusion-filtered diff (capped at 150KB with a truncation note), and the verdict-format block. Replaces the six-step manual assembly procedure in `goal-judge/SKILL.md`.
- `gk doctor [--fix]` — detects and repairs the interrupted-advance failure shapes (stale active.json, missing next-link state, approved-but-not-advanced cursor, missing link_approvals). Replaces the Symptoms A–D recovery prose.
- `gk log <slug> --compact` — activation entry + every judge/lifecycle block + last N checkpoints, with an omission marker. Used for executor re-spawns, judge briefs, and supervisor input, so spawn prompts no longer grow monotonically with the log.
- `gk baseline <slug>` — pre-activation validator run recorded to `baseline.json`; merged into `state.json` at activation (replaces prep's manual baseline capture).
- `gk resume` from `needs_human` **requires** `--reset-rejections` or `--keep-count` — the CLI itself enforces that the user was asked.
- `scripts/test-gk.py` — 82-assertion end-to-end suite driving gk as a subprocess through real lifecycles in throwaway git repos: standalone goal, max-rejections/needs_human gating, chain start→advance→complete, clear-aborts-chain, baseline merge, judge-brief content, log compaction, doctor repair, and hook-guard block/allow/fail-open behavior. Wired into CI.

### Changed

- All eight SKILL.md files rewritten or trimmed to delegate mechanics to gk. Net effect: skills describe judgment and orchestration; no skill hand-writes a state file or log entry anymore. The "Canonical state shapes" section moved out of `goal/SKILL.md` — `scripts/gk.py` is now the single source of truth for shapes (asserted by both test suites).
- Judge hardening: every MET verdict in the judge's REASONS must now cite `file:line` (or a named file section) as evidence — a MET without a citation is invalid. `goal-judge/SKILL.md` also documents an optional **multi-lens panel** for high-stakes gates (placeholder-hunter / non-goals auditor / behavior verifier run in parallel; approve only if all approve).
- Executor-subagent directive (`goal-chain/SKILL.md`) now instructs executors to checkpoint and validate via gk — required, since the hook would block their direct log edits.
- Removed the stale "Cache-aware wakeup delay" section (written against the old 5-minute prompt-cache TTL, which no longer holds). Pacing now defers to the contract's `wakeup_seconds` and the harness's own live ScheduleWakeup guidance.

### Removed

- `goal-chain/SKILL.md` "Advance mode" and "Recovery from interrupted advance" sections — superseded by `gk verdict`'s atomic advance and `gk doctor`.

### Migration notes

- **State-file compatible.** gk reads and writes the same `state.json` / `active.json` / `chain.json` / `log.md` shapes as v0.3; an in-flight v0.3 goal or chain continues under v0.4 (run `gk doctor` once if it was interrupted mid-advance).
- **Contracts unchanged.** All existing contract.md files remain valid; the schema is untouched.
- **Hook activation requires a plugin reload** (disable/enable or restart) — hooks are read at startup.

## [0.3.0] - 2026-05-11

**Executor-subagent chain execution.** Chains now run each goal's implementation work in a fresh-context subagent instead of the main conversation. Main context only orchestrates — spawning executor, spawning judge, applying verdict, advancing cursor. This is the load-bearing change that lets multi-goal chains run autonomously without main-context aging out.

### Why

In v0.2, `/goal-chain` advanced through goals by re-entering the `/goal` skill execution loop in the SAME conversation context. Each goal's work — file edits, validator runs, debugging — accumulated in main context. A typical 8-9 goal chain would burn through context budget around goal 2-3 and force the user to start fresh sessions per goal, defeating the point of "linear sequence with judge gates."

The judge-as-subagent pattern v0.2 already had was half the answer. v0.3 extends it: executor is ALSO a subagent. Main context cost per goal drops from "all the implementation work" (tens of thousands of tokens accumulating) to "executor return summary + judge spawn" (~10K tokens, flat). A 9-goal chain that used to need 9 sessions can run in one.

### Changed

- `skills/goal-chain/SKILL.md` Step 6 (Start mode "Hand off to the goal skill") and Advance-mode next-link path:
  - **Before:** "Re-enter the goal skill execution loop on the activated slug. Real work begins immediately in this turn."
  - **After:** Spawn a fresh-context executor subagent via the Agent tool. Pass contract + log + chain context + repo state + a structured executor directive. The subagent does the implementation work end-to-end, runs the validator, and returns a structured summary (STATUS / SUMMARY / VALIDATOR_OUTPUT_TAIL / FILES_CHANGED / BLOCKERS). Main context then spawns the judge subagent, applies the verdict, and either advances cursor or re-spawns the executor with the judge's fix-list (within rejection budget).
- `skills/goal/SKILL.md`:
  - New top-level **Execution modes** section explaining inline (standalone `/goal`) vs subagent (chain-driven) execution. Both modes use the same state.json / log.md / active.json files and the same Execution Loop protocol — the only difference is who runs it.
  - Execution Loop step 7 (Branch on validator) now branches per mode: inline mode invokes judge directly + uses ScheduleWakeup; subagent mode returns to chain orchestrator with structured summary.
  - Execution Loop step 8 (Branch on judge verdict) marked as inline-mode-only. In subagent mode, the chain orchestrator handles this section.
- `skills/goal-judge/SKILL.md`:
  - Frontmatter description and new "Invocation sources" section explain three legitimate entry points: inline auto-fire from `/goal`, chain-orchestrator invocation after executor return, and on-demand `/goal-judge` for advisory review.
  - Inputs section gains a "Subagent-mode extra context" note: when invoked by `/goal-chain`, the judge receives the executor's structured summary as a leading hint but MUST still independently verify against the contract — executor self-reports are not authoritative.

### Migration notes

- **Backward compatible at the state-file level.** chain.json / state.json / active.json / log.md shapes are unchanged. A v0.2 chain in progress can be resumed under v0.3 — the cursor advances correctly; only the execution mechanism differs.
- **No breaking changes to contracts.** All v0.2 contract.md files remain valid. The validate-contracts.py schema is unchanged.
- **Existing inline `/goal` usage unchanged.** Standalone `/goal "<objective>"` invocations still run in main context with ScheduleWakeup pacing. v0.3 only affects chain-driven execution.
- **Chains now naturally finish in one session.** The recommended pattern is: prep all contracts in one session, then `/goal-chain` once and let it run all goals to completion in a single main-context conversation.

### Design decisions worth flagging

- **Executor subagent is fresh context, no conversation history.** This forces contracts to be fully self-contained at prep time. Any goal contract with checkpoint-1 questions ("ask user about X") is a smell; resolve those at prep, not during execution.
- **Judge is still spawned by main context, not by executor.** This preserves the invariant that the judge sees an independent view (executor's diff + log + validator output) and reasons fresh. If the executor could spawn its own judge, the judge would be in the executor's sub-sub-context, with shared-rationalization risk.
- **Reject + re-spawn cycle stays within main context.** If the judge rejects, main context re-spawns the executor with the original contract + log (which now includes the judge's fix-list) + a directive to address the fix-list. The executor runs in a fresh subagent each time, but the rejection_count and orchestration live in main. This caps loops at `max_rejections` without burning main-context budget on internal iterations.
- **Subagent-mode does not need ScheduleWakeup.** The subagent runs end-to-end in one turn. ScheduleWakeup was an inline-mode concept for pacing long-running validators across main-conversation turns; subagents handle long validators within their own turn.

### Discovery context

The change was driven by hitting the v0.2 ceiling on a real 9-goal chain (v2-shadow-prod-ready in `agently-ai`). After 2 successful goal completions in the same main-conversation context, context budget became the constraint. The fix wasn't "smaller goals" or "more sessions" — it was extending the subagent pattern that v0.2 had already half-implemented for judges. See the chain's mission-log.md in that repo for the live discovery + decision.

## [0.2.0] - 2026-05-11

**New primitive: missions.** One level above goals. Where the judge gates a goal against its Definition of Done, the supervisor gates a *mission* against its charter and adaptively decides what goal to run next based on the prior goal's actual output. Built because the existing chain primitive commits to a linear sequence at chain-start, which doesn't fit arcs where the shape of step N+1 depends on what step N actually produced (e.g., V2 cutovers, multi-phase migrations, anything with branch-or-iterate decisions between goals).

### Added

- `skills/goal-supervisor/SKILL.md` — new skill `/goalkeeper:goal-supervisor`. Invoke after any goal completes; reads `.claude/mission.md` + the just-ended goal's artifacts; spawns a fresh-context subagent that returns `PROCEED`/`DONE`/`ESCALATE`. On `PROCEED` it hands off to `/goalkeeper:goal-prep` with a drafted next-objective; on `DONE` it writes `.claude/mission-completed.md` with success-condition evidence; on `ESCALATE` it surfaces the ambiguity to the user.
- New mission-layer files (all under `.claude/`, NOT auto-gitignored — author your charter the way you want it tracked):
  - `mission.md` — user-authored charter (objective, success condition, constraints, legal next-goal shapes, "done is not" non-goals). Required for supervisor to run.
  - `mission.json` — supervisor state (status, goals_completed[], supervisor_verdicts[]).
  - `mission-log.md` — append-only mission-level audit trail.
  - `mission-completed.md` — final snapshot written on supervisor `DONE`.
- `skills/goal/SKILL.md` canonical-state-shapes section gained the `mission.json` schema.
- `scripts/test-lifecycle.py` — 3 new tests for supervisor state transitions (`PROCEED`/`DONE`/`ESCALATE`). 80/80 assertions across 14 tests.
- `README.md` — new "Missions — supervised iterative arcs" section explaining the primitive and when to reach for missions vs chains.

### Design decisions worth flagging

- **Supervisor's `PROCEED` drafts but does NOT auto-activate.** The drafted next-goal contract goes through `/goalkeeper:goal-prep`'s user-review flow before activation. The human-in-the-loop checkpoint at prep is the safety property; bypassing it is a v0.3 conversation, not v0.2 default.
- **Supervisor is NOT a chain.** Chains commit linearly at start. Missions adaptively decide each next step. Documentation pushes hard on picking the right primitive — chains for sequenceable work known up front, missions for adaptive arcs.
- **Fresh-context supervisor subagent.** Same discipline as the judge: independent reasoning, no shared rationalization with the executing or prior agents. Output ONCE, no self-correction, structured verdict format.
- **One supervisor invocation per goal-completion.** Re-invoking without a new completed goal is a no-op (returns last verdict). Prevents runaway loops.
- **Append-only mission-log.** Same invariant as per-goal logs.
- **No mission DAG / branching in v0.2.** Linear arcs only. DAG missions are a v0.3+ conversation.

### Backward compat

- Existing chains, goals, contracts, validators, judges: unchanged. No spec edits to `goal/SKILL.md`, `goal-prep/SKILL.md`, `goal-judge/SKILL.md`, `goal-chain/SKILL.md`, `goal-pause/SKILL.md`, `goal-resume/SKILL.md`, or `goal-clear/SKILL.md` beyond adding the `mission.json` schema reference.
- `/goalkeeper:goal-supervisor` is opt-in: if `mission.md` doesn't exist, the skill halts with a usage message. Users who never want missions can ignore the feature entirely.
- Plugin version bumps from `0.1.11` to `0.2.0` because this adds a new primitive and a new skill. No breaking changes — all 0.1.x contracts/chains/state shapes continue to work unmodified.

## [0.1.11] - 2026-05-11

Tiny fix surfaced while staging a goalkeeper demo on a separate repo
(idbro). `scripts/validate-contracts.py` crashed with `ValueError: ... is
not in the subpath of ...` when given an explicit path argument pointing
at a contract outside the goalkeeper repo.

### Fixed

- `scripts/validate-contracts.py` — falls back to the absolute path when
  a target file is outside `REPO_ROOT` instead of crashing. Repo-internal
  files still display as relative paths (tidy output). Lets the script
  validate contracts in other repos when called with explicit CLI args:
  `python3 scripts/validate-contracts.py /other/repo/.claude/goals/foo/contract.md`.

## [0.1.10] - 2026-05-10

Pre-launch branding pass. No skill behavior or spec changes — purely
README hero polish and ship-ready brand assets.

### Added

- `branding/mark.svg` — icon mark (64×64). Three vertical goal-posts
  topped by a crossbar; reads as both soccer goal and judicial gate.
- `branding/wordmark.svg` — lowercase monospace wordmark (480×96).
- `branding/lockup.svg` — mark + wordmark (640×96). The "logo proper."
- `branding/social-card.svg` — 1200×630 OG image for Twitter / Slack /
  Discord link unfurls. Tagline: "Set durable goals. Approve at the gate."
- `branding/README.md` — usage notes, color palette, typography stack,
  SVG→PNG conversion recipes, and don't-do list.
- README "Brand" section linking to the brand kit.
- README footer mark.

### Changed

- README hero rewritten:
  - Lockup image above the fold
  - Tagline as h3 subtitle: "Set durable goals. Approve at the gate."
  - Badge row: test (CI), license, version, claude-code-plugin
  - **Comparison table** showing goalkeeper vs OpenAI Codex `/goal` vs
    Ralph loop across 9 dimensions (durable goals, validator, judge
    gate, anti-placeholder, chains, auto-pause, append-only log, spec
    location, validator-baseline subtraction). The judge column is the
    elevator pitch.
  - "Inspired by..." moved below the comparison so the differentiation
    is visible first.
- README link to canonical state shapes updated from `skills/goal.md`
  to `skills/goal/SKILL.md` (path fix after the v0.1.6 layout change).

### Brand kit

- **Primary color:** `#0E7C66` (deep teal-emerald)
- **Typography (wordmark):** monospace stack — `ui-monospace,
  'JetBrains Mono', 'SF Mono', Menlo, Consolas`
- **Typography (headlines):** system sans
- **Tagline:** "Set durable goals. Approve at the gate."

Drafts and alternate concepts live in
`~/Documents/context-relay/projects/goalkeeper/branding/` (not in
this repo per the hybrid branding-placement decision).



Spec extension driven by the v2-shadow-pipeline dogfood on the
agently-ai codebase: the contract's validator was `npm run test &&
npm run test:e2e && npm run lint`, and lint was already failing at
the baseline commit on two files the goal never touched. Every
checkpoint's validator run would have failed for reasons unrelated
to the goal's work, polluting the judge's verdict. v0.1.9 adds the
mechanism to detect and subtract this pre-existing debt.

### Added

- `state.json` schema (canonical, in `skills/goal/SKILL.md`) gained two
  optional fields:
  - `validator_baseline_result`: `"pass" | "fail" | "not_runnable" | null`
  - `validator_baseline_failing_paths`: array of paths the validator
    was already failing on at activation

### Changed

- `skills/goal-prep/SKILL.md` — when prep runs the validator once to
  confirm it executes, it now also captures the exit code AND the list
  of failing file paths from the output. Both flow into the activation
  state.json so the judge can subtract pre-existing failures.
- `skills/goal/SKILL.md` — activation step 3 picks up the
  validator baseline from prep (when present) and writes it into
  state.json alongside the existing `started_at_commit` /
  `started_at_dirty_paths`.
- `skills/goal-judge/SKILL.md` — Inputs section names the two new
  state fields. "Pre-existing-dirt subtraction" gained a sibling
  section ("Pre-existing validator-failure subtraction") spelling out:
  - Goal-caused failure: validator passed at baseline but fails now,
    OR fails on a path the goal modified that wasn't in
    `validator_baseline_failing_paths`. Blocks approval.
  - Pre-existing failure: validator was already failing on a path the
    goal didn't modify. Does NOT block approval; surface in NOTES.
- `skills/goal-judge/SKILL.md` — subagent prompt template now
  includes the validator baseline + pre-existing-failing-paths in the
  "Diff scope" header, plus a new "Pre-existing validator-failure
  check" line in the structured-response format.
- `README.md` — "State and storage" section documents the two new
  optional state fields.

### Why this matters

Without this mechanism: any contract whose validator chains multiple
commands (`test && lint && typecheck && ...`) is at the mercy of every
unrelated failure in the codebase. The judge would reject every
checkpoint until the entire repo is clean, which makes goalkeeper
unusable on real codebases with pre-existing debt.

With this mechanism: the goal proceeds. The judge correctly
distinguishes "this goal broke X" from "X was already broken." The
user can opportunistically fix the pre-existing debt or amend the
contract to scope the validator to changed files — informed by the
judge's NOTES section.

### Verified

- **Local install end-to-end on developer machine 2026-05-10:**
  `claude plugin validate .` passes; `claude plugin marketplace add
  ~/Documents/goalkeeper` succeeds; `claude plugin install
  goalkeeper@goalkeeper` produces v0.1.8 install at
  `~/.claude/plugins/cache/goalkeeper/goalkeeper/0.1.8/` with all 7
  SKILL.md files discovered and the plugin shown as `✔ enabled` in
  `claude plugin list`. Strongest possible release-readiness signal
  short of in-session skill invocation (which requires a Claude Code
  session restart).

### Added

- `scripts/test-lifecycle.py` — 10th test (`test_judge_advisory_no_state_change`)
  covering the goal-judge.md "advisory mode" invariant: when invoked
  on-demand outside the auto-gate flow, the judge does NOT modify
  state.json, rejection_count, or active.json. Closes the previously-
  noted "inline judge mode never tested" gap. Total now 59 assertions
  across 10 tests.

## [0.1.8] - 2026-05-10

### Added

- `.github/workflows/test.yml` — GitHub Actions workflow running on
  every push to main and every PR. Steps:
  1. Install pyyaml + jsonschema.
  2. Run `scripts/validate-contracts.py` (every contract conforms
     to the schema).
  3. Run `scripts/test-lifecycle.py` (54-assertion state-machine suite).
  4. JSON-lint the manifest files (plugin.json, marketplace.json,
     contract.schema.json).
  5. Verify each `skills/<name>/SKILL.md` exists with frontmatter
     declaring `name: <name>` matching the directory.

### Why

This locks in the regression protection from v0.1.4 (schema validity)
and v0.1.7 (lifecycle suite) so future contributors can't accidentally
ship a YAML-broken contract or a SKILL.md naming mismatch. The skill-
naming check would have caught the v0.1.6 layout mistake at PR time
instead of release time.



### Added

- `scripts/test-lifecycle.py` — re-runnable spec-conformance suite
  encoding 54 assertions across 9 state transitions. Closes the v0.1.5
  "deferred to v0.2" gap. Stdlib-only (no extra deps), runs in seconds,
  exits non-zero on any failure. Suitable for CI / pre-commit. Tests:
  - `/goal-pause`: active → paused
  - `/goal-resume`: paused → active
  - `/goal-clear`: active → cleared + archived
  - `/goal-resume` from needs_human with rejection counter reset
  - max_rejections threshold (4 → 5 → needs_human)
  - chain completion (cursor reaches end → chain done)
  - `/goal-clear` during active chain (chain.status flips to aborted,
    unreached link contracts preserved)
  - judge approve on standalone goal (terminal active.json)
  - judge reject below threshold (rejection_count++, stays active)
- `CONTRIBUTING.md` — added "Running the lifecycle suite" pointer.

### Notes

This is a *spec consistency* test, not a Claude Code skill integration
test — it codifies the canonical state shapes from
`skills/goal/SKILL.md` and verifies that constructing them per spec
produces the expected results. If a SKILL.md ever drifts from the
canonical shapes documented in `goal`, this test catches the
divergence on the next run.



**Plumbing release — required for `/plugin install` to work.** Verified
against installed plugins on disk (caveman, ralph-loop) before
restructure. No skill behavior changes.

### Changed

- Skills restructured from flat `skills/<name>.md` to the official
  Claude Code plugin layout `skills/<name>/SKILL.md`. All 7 skills
  moved via `git mv` to preserve history. Required because Claude
  Code discovers skills at `<plugin-root>/skills/<name>/SKILL.md`
  and ignores flat `.md` files inside `skills/`.
- `README.md` install section rewritten. The previous instructions
  (`/plugin install itsuzef/goalkeeper`) wouldn't have worked —
  Claude Code requires marketplace registration first. New flow:
  ```bash
  /plugin marketplace add itsuzef/goalkeeper
  /plugin install goalkeeper@goalkeeper
  ```
- README documents the namespace-prefix invocation pattern
  (`/goalkeeper:goal "<obj>"`) and a recommended `~/.claude/settings.json`
  alias block for users who want shorter commands.

### Added

- `.claude-plugin/marketplace.json` — required for `/plugin marketplace
  add` discovery. Lists goalkeeper as a single-plugin marketplace
  pointing at the repo root. Modeled on the structure of installed
  plugins on the developer's machine.

### Why this wasn't caught earlier

The four prior dogfood runs (chain, rejection cycle, lifecycle,
schema audit) were all manual walkthroughs of the skill *content* —
they verified that the skill markdown bodies produced correct state
transitions when followed. None of them exercised Claude Code's
*discovery* of those skills, because we were running the skills
manually rather than installing the plugin. The fifth release-readiness
gate was "compare on-disk structure to a real installed plugin," which
caught the layout divergence.



Verification release: comprehensive lifecycle dogfood (pause / resume /
resume-from-needs_human / clear / chain-abort) revealed one small spec
gap, fixed here. No skill behavior changes from a user perspective.

### Changed

- `skills/goal-clear.md` — clarified the chain-handling step:
  - The chain-level log entry on abort goes to the cleared slug's
    own `log.md` (chain-level events are reconstructible from
    per-link logs + chain.json; no separate chain-log file).
  - Explicitly addressed the `chain.json.status` already-`done`/-`aborted`
    case: leave chain.json untouched (you're archiving a goal whose
    chain finished earlier, not aborting in-flight).
  - Reaffirmed: unreached chain-link contracts stay in place for
    re-use.
  - Spelled out the `cleared` (per-goal) vs `aborted` (chain-level)
    distinction so the two terminal flags don't get conflated.

### Verified by dogfood

Lifecycle dogfood ran every state transition with synthetic fixtures
and hand-authored Python assertions:

- **/goal-pause**: active → paused (7 assertions PASS).
- **/goal-resume from paused**: paused → active (5 assertions PASS).
- **/goal-clear**: active → cleared with archive flow (10 assertions
  PASS — directory moved not deleted, all state files preserved,
  active.json terminal shape correct).
- **/goal-resume from needs_human with counter reset**: needs_human →
  active, rejection_count 5 → 0, audit-trail timestamps preserved (7
  assertions PASS).
- **/goal-clear during active chain**: active → cleared, chain.status
  flipped to aborted, link 1 archived, unreached link 2 contract
  preserved (11 assertions PASS — including the critical distinction
  that active.json `ended_reason="cleared"` while chain.json
  `status="aborted"`).
- **max_rejections threshold**: synthetic test with rejection_count=4
  → simulated 5th reject → status flipped to needs_human, log
  captured paused entry, active.json correctly stayed in active
  shape (7 assertions PASS).

Total: 47 lifecycle assertions across 6 transition paths, all PASS.

### Known coverage gaps (deferred)

- 3 alternate user-choice paths in `/goal-resume` from needs_human:
  Continue without reset, Abandon-via-resume.
- `/goal-pause` when already paused (should no-op per spec).
- `/goal-clear` when no active goal (should tell user and stop).
- `/goal-resume` from `done` state (should refuse per spec).

Each is a mechanical no-op or refusal path. v0.2 may add a
`scripts/test-lifecycle.py` covering all transitions automatically.



Driven by a schema-validity audit run during the rejection-cycle dogfood.
Every contract in the repo now validates against `contract.schema.json`,
and there's a script for users to run the same check locally.

### Added

- `scripts/validate-contracts.py` — walks the repo, finds every
  `*.md` with frontmatter declaring a `slug`, and validates it against
  `schemas/contract.schema.json` (using `pyyaml` and `jsonschema`).
  Skips files without a `slug` field (chain files, skill files,
  README, etc.) unless `--strict` is passed. Exits non-zero on any
  validation failure, suitable for CI / pre-commit.
- `CONTRIBUTING.md` — new "YAML pitfalls" subsection in "Writing a
  contract" calling out the two patterns that broke contracts in the
  audit: bullets starting with a quoted phrase, and unquoted strings
  containing colons inside quoted phrases. Both are caught by
  `validate-contracts.py`.

### Fixed

- `examples/migration.md` — DoD bullet that started with `"pnpm
  test"` (broke YAML parsing because the bullet's value was a
  quoted scalar followed by unparseable trailing text). Rewritten as
  `The "pnpm test" command runs ...`. Discovered when running the
  new `validate-contracts.py` against the repo.



Spec-tightening release driven by findings from the rejection-cycle
dogfood (one natural reject-then-fix-then-approve cycle on a
contributing-md goal, plus a synthetic max_rejections threshold test).

### Added

- `skills/goal.md` — `needs_human_at` timestamp added to the canonical
  state.json schema, parallel to `approved_at` / `paused_at` /
  `resumed_at`. Populated when `rejection_count` reaches
  `max_rejections` and status flips to `needs_human`.
- `CONTRIBUTING.md` — first contributor guide. Sections cover
  adding a skill, writing a contract, and running a dogfood loop
  with a concrete worked-example session. Produced by the
  rejection-cycle dogfood (rejected on first pass for a placeholder,
  approved on second pass after the fix-list was addressed).

### Changed

- `skills/goal-judge.md` — subagent prompt template gained an
  explicit "Output the verdict ONCE. Pre-think your reasoning before
  producing the structured response. Do not self-correct or revise
  individual DoD lines mid-response" instruction. Removes the
  in-response self-correction observed during the first reject-cycle
  judgment (judge marked one DoD NOT MET then corrected to MET inside
  the same verdict).
- `skills/goal-judge.md` — `On reject` threshold-check step now
  explicitly sets `state.needs_human_at` and clarifies that
  `active.json` is NOT touched on `needs_human` (the goal is paused
  awaiting human input, not terminated).

### Verified by dogfood

- Validator-vs-judge separation works as designed: a validator-
  passing file does not mean a judge-approving file. The contributing-
  md goal had a loose validator (file existence + section headers)
  and a strict DoD (no placeholders, concrete examples). First pass
  passed validator and was rejected by the judge; second pass passed
  both.
- Judge correctly distinguishes literal placeholders from rule-
  documentation references (e.g. "avoid 'TODO: real implementation'
  patterns" is rule discussion, not a violation).
- Fix-list flow works: executing agent reads the latest "judge
  rejected" block from log.md and addresses each item before re-
  judging. log.md is functional as the message bus between
  iterations.
- max_rejections threshold logic is correct: synthetic test with
  rejection_count=4 → simulated 5th reject → state.status correctly
  flipped to `needs_human`, log captured the paused entry, active.json
  correctly stayed in active shape.



Spec-tightening release driven by findings from the second dogfood run
(`/goal-chain` end-to-end on the goalkeeper repo itself, 2 links, both
judge-approved).

### Changed

- `skills/goal.md` — added a "Canonical state shapes" section as the
  single source of truth for `active.json`, `<slug>/state.json`, and
  `chain.json` schemas. Other skills now reference this section instead
  of inlining their own divergent shapes.
- `active.json` schema converged on two shapes only: **active**
  (`{slug, activated_at, chain?}`) or **terminal**
  (`{slug: null, ended_at, ended_reason, previous_slug?, previous_chain?}`).
  Previously each terminal flow (done, cleared, chain_completed) wrote
  a different ad-hoc shape.
- `skills/goal-clear.md` — clear flow writes the canonical terminal
  shape with `ended_reason: "cleared"`. When clearing during a chain,
  also includes `previous_chain`; the chain.json carries the abort
  signal separately.
- `skills/goal-judge.md` — on-approve standalone path writes the
  canonical terminal shape with `ended_reason: "done"`.
- `skills/goal-judge.md` — replaced the prose "Compute the diff
  scope" section with a 5-step mechanical assembly procedure
  containing exact shell command templates (`git diff
  <baseline>..HEAD`, `git ls-files --others --exclude-standard`, etc.)
  and a copy-pasteable `DEFAULT_EXCLUDES` array. Removes inconsistency
  risk between judge invocations.
- `skills/goal-chain.md` — chain-completion path writes the canonical
  terminal shape with `ended_reason: "chain_completed"` plus
  `previous_chain` and `previous_slug`.
- `skills/goal-chain.md` — chain advance now documents an atomic write
  order (mark previous done → record link approval → increment cursor
  → initialize next state.json + log.md → update active.json LAST) so
  interruptions leave a recoverable state.
- `skills/goal-chain.md` — status-mode print format now includes
  per-link approval timestamps from `chain.json.link_approvals[]`,
  plus chain-level started_at / completed_at lines.

### Added

- `chain.json` schema gained `link_approvals: [{slug, approved_at}]`.
  Initialized empty by `/goal-chain` start; the goal-judge skill
  appends one entry per approved chain link before handing off to
  goal-chain advance. Provides chain-level visibility without having
  to walk per-link `state.json` files.
- `skills/goal-chain.md` — new "Recovery from interrupted advance"
  section documenting four symptoms of a half-failed advance (cursor
  advanced but active.json stale; cursor advanced but next state.json
  missing; previous done but cursor not advanced; missing
  link_approvals entry) and the corresponding manual recovery for
  each.
- `README.md` — "State and storage" section now documents the
  canonical shapes for `active.json`, `state.json`, and `chain.json`
  with cross-reference to `skills/goal.md` as the source of truth.

### Fixed

- The three previously-divergent terminal `active.json` shapes
  (`{slug: null, completed_at}`, `{slug: null, cleared_at}`,
  `{slug: null, chain_completed_at}`) are now unified, eliminating a
  class of cross-skill bugs where one skill wrote a shape another
  skill couldn't read.



### Changed

- `skills/goal-judge.md` — judge subagent prompt now instructs the agent to
  Read each modified or added file end-to-end via the Read tool, not just
  trust the diff. Diffs lose context (renames, surrounding code, file-level
  structure) and were letting placeholder patterns slip through during the
  first dogfood run.
- `skills/goal-judge.md` — judge now applies a default exclusion list
  (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`,
  `poetry.lock`, `go.sum`, `Gemfile.lock`, `composer.lock`, `dist/`,
  `build/`, `out/`, `target/`, `.next/`, `*.min.js`, `*.min.css`,
  `coverage/`, `.nyc_output/`, `test-results/`, `.vscode/`, `.idea/`,
  `.DS_Store`) so auto-generated noise stays out of the judge's context.
- `skills/goal-judge.md` — judge verdict format extended with an
  explicit `Pre-existing-dirt check` line so changes to paths that
  were already dirty at activation are flagged for review rather than
  silently credited as goal work.

### Added

- `skills/goal.md` — goal activation now captures `git rev-parse HEAD`
  and `git status --porcelain` into `state.started_at_commit` and
  `state.started_at_dirty_paths`. The judge uses these as the diff
  origin and pre-existing-dirt baseline.
- `schemas/contract.schema.json` — optional `diff_excludes: string[]`
  field for per-repo noise globs that should be excluded from the
  judge's diff in addition to the defaults.
- `README.md` — documents the new `diff_excludes` contract field and
  the `started_at_commit` state field.

## [0.1.0] - 2026-05-10

### Added

- Initial public release of the **goalkeeper** Claude Code plugin.
- Plugin manifest at `.claude-plugin/plugin.json`.
- Skill: `/goal "<objective>"` — set or check status of a durable
  contract-driven goal. Auto-routes to `/goal-prep` when no contract
  exists for the slug.
- Skill: `/goal-prep "<rough idea>"` — interactive contract drafter.
  Surveys the repo and uses targeted questions to lock in objective,
  Definition of Done, validator command, and non-goals before
  execution begins.
- Skill: `/goal-pause` — pause an active goal without losing state.
- Skill: `/goal-resume` — resume a paused or `needs_human` goal,
  with explicit confirmation before resetting the rejection counter.
- Skill: `/goal-clear` — stop and archive the active goal to
  `.claude/goals/_archive/`. Files are moved, never deleted.
- Skill: `/goal-judge` — the gate. Default subagent mode spawns a
  fresh-context agent that reviews the diff and progress log against
  the contract's Definition of Done and returns approve / reject
  with a structured fix-list. Inline mode is available for cheap
  advisory iteration but not for chain gating.
- Skill: `/goal-chain "<file>"` — run a linear sequence of goals
  with judge-gated progression between them. The judge approves
  each link before the next starts.
- Contract schema at `schemas/contract.schema.json` (JSON Schema
  draft 2020-12) covering objective, non_goals, definition_of_done,
  validator, checkpoint_cadence, max_rejections, judge_mode,
  wakeup_seconds.
- Three example contracts under `examples/`: a Jest-to-Vitest
  migration, an iterative prompt-optimization run, and a four-step
  chain.
- `README.md` covering install, quick start, command reference,
  contract format, chain definition, state-and-storage layout, and
  design notes.
- MIT `LICENSE`.
- Anti-placeholder rule borrowed from the Ralph loop: stubs, mocks,
  `.todo`, `.skip`, and "TODO: real implementation" patterns are
  automatic judge rejection regardless of validator status.
- Cache-aware wakeup-delay guidance (60-270s for tight loops,
  1200-1800s for slow validators or idle waits, avoid 300s) so
  autonomous loops don't burn the Anthropic prompt cache TTL.

[Unreleased]: https://github.com/bonfire-systems/goalkeeper/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/bonfire-systems/goalkeeper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/bonfire-systems/goalkeeper/releases/tag/v0.1.0
