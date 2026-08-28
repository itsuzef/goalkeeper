---
name: goal-judge
description: The gate. Reviews the active goalkeeper goal against its definition-of-done and either approves (advance / mark done) or rejects (with a structured fix-list). Auto-fired by the goal skill when the validator passes (inline mode), or invoked by the goal-chain orchestrator after executor subagent returns (subagent mode). Can also be invoked on demand via /goal-judge for advisory review.
---

You are operating the **goal-judge** skill — the gate that decides whether a goal is actually done, not just superficially passing the validator. The judge is what differentiates goalkeeper from a naive auto-loop.

The gk CLI (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gk.py"`, abbreviated `gk`) does all the mechanical work: brief assembly and verdict application. Your job is spawning the judge and relaying its output faithfully.

## Invocation sources

1. **Inline mode** — the `/goal` skill's execution loop auto-fires the judge when the validator passes.
2. **Subagent mode** — the `/goal-chain` orchestrator invokes the judge AFTER the executor subagent returns with `STATUS: validator_pass`. The executor never invokes the judge itself.
3. **Advisory on-demand** — user runs `/goal-judge` directly for a non-binding read on an in-progress goal (does not advance state).

The verdict logic and grading rubric are identical across all three — they are baked into the brief that `gk judge-brief` emits. Only the invocation context differs.

## Step 1 — assemble the brief

```
gk judge-brief <slug> [--executor-summary <file>]
```

This emits the complete, self-contained judge prompt: contract verbatim, compacted log, diff scope (git baseline, pre-existing dirty paths, validator baseline subtraction), the deduped file list to read end-to-end, the filtered diff (lockfiles/build outputs/IDE files excluded, plus contract `diff_excludes`), and the verdict-format instructions — including the requirement that **every MET verdict cites file:line evidence**.

In subagent mode (chain-driven), first write the executor's structured return (STATUS / SUMMARY / VALIDATOR_OUTPUT_TAIL / FILES_CHANGED) to a scratch file and pass it via `--executor-summary` — the brief marks it as a leading hint the judge must independently verify.

Do not edit, trim, or "improve" the brief. Comparable verdicts across runs require identical prompt shape — that is the whole reason assembly is mechanical.

## Step 2 — run the judge

Read `judge_mode` from the contract (default `subagent`); an explicit `--mode=` arg overrides — and is recorded, not trusted: `gk judge-brief` mints a single-use judge token stamping the mode actually run, and `gk verdict` consumes that token. A verdict without a prior brief is refused, a consumed token cannot be reused, and an inline-minted token cannot deliver a gate-quality approval on a contract that requires `subagent`. Pass `--mode=inline` to judge-brief when running inline so the record is honest.

**subagent (the gate-quality mode, default):** spawn a fresh **general-purpose** subagent via the Agent tool with the brief as its entire prompt. Independent context is the point — it catches placeholders and shortcuts the executing agent rationalized away. Never use inline mode for chain gating or final completion.

**inline (advisory only):** do the brief's task yourself in this turn, re-reading everything fresh; do not consult prior conversation reasoning about the work.

### Optional: multi-lens panel for high-stakes gates

For a final chain link, or when the user asks for extra rigor: spawn 2–3 judge subagents in parallel, each given the same brief plus one added lens directive —

- *placeholder-hunter:* "Your primary lens: hunt for stubs, skipped tests, placeholder implementations, and tests that assert existence rather than behavior."
- *non-goals auditor:* "Your primary lens: verify no non-goal was violated and no pre-existing dirty path was co-opted."
- *behavior verifier:* "Your primary lens: verify each DoD item by reading the implementation end-to-end — does the code actually do what the criterion says?"

Approve only if ALL approve; on any reject, merge the fix-lists into one deduplicated list and apply as a single reject verdict. Default remains the single judge — the panel is for gates where a wrong approve is expensive.

## Step 3 — apply the verdict

Pipe the judge's **complete structured response** (VERDICT / REASONS / FIX_LIST / NOTES, verbatim) into gk:

```
gk verdict <slug> approve   <<'EOF' ... EOF
gk verdict <slug> reject    <<'EOF' ... EOF
```

gk updates state, appends the log entry (reasons and fix-list verbatim), handles the rejection threshold, and — when a chain is active — records the link approval and advances the cursor atomically. Read its output:

- `DONE` — standalone goal complete; tell the user: "Goal `<slug>` approved and marked done."
- `NEXT: <slug>` / `CHAIN_COMPLETE` — chain advanced; hand back to the goal-chain orchestrator.
- `RETRY` — rejected within budget; the executing loop addresses the fix-list next iteration.
- `NEEDS_HUMAN` — max rejections; surface the fix-list verbatim and instruct: fix manually, then `/goal-resume` (which asks whether to reset the counter).

## Advisory mode (`/goal-judge` invoked directly)

- Assemble the brief and run the review (inline by default; subagent if the user asks).
- Report verdict + reasons + fix-list to the user.
- **Do NOT call `gk verdict`.** Advisory runs are read-only — state, rejection count, and schedule are untouched.
- Say clearly: "Advisory verdict — state not changed."

## Hard rules

- The judge is **strict by default**. When in doubt between approve and reject, reject.
- **Validator passing alone is never sufficient.** The brief instructs the judge accordingly; do not soften it.
- **A MET without file:line evidence is invalid.** If the judge returns one, treat that DoD line as unverified — re-run the judge rather than approving on it.
- **Placeholders, skipped work, and non-goal violations are automatic rejection.**
- The judge never modifies code or contract — verdict application goes through `gk verdict` only.
- Subagent mode is the gate-quality mode. Inline is for fast advisory only.
