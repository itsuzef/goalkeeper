---
name: goal-prep
description: Interactively draft a goalkeeper contract before executing. Use this skill when the user invokes /goal-prep "<rough idea>", or when /goal is called for a slug that has no contract yet. Produces a well-formed contract.md with explicit objective, definition-of-done, validator command, and non-goals.
---

You are operating the **goal-prep** skill. Your job is to turn a rough idea into a precise, executable goalkeeper contract. A bad contract is goalkeeper's #1 failure mode — your work here is the highest-leverage step in the whole flow.

## Inputs

- `args` (rough idea, may be empty)
- The current repo (you can read code, configs, docs)

## Flow

### 1. Reconnaissance (read-only)

Before drafting anything, briefly survey the repo to ground the contract in reality:

- Detect language/framework: package.json, pyproject.toml, Cargo.toml, go.mod, *.csproj, etc.
- Find the test/lint/build commands the project actually uses (CI workflow files, Makefile, package.json scripts).
- Skim the README and any AGENTS.md / CLAUDE.md for project conventions.
- If the rough idea points to specific files or features, locate them.

Spend ~2–5 read-only tool calls here. Do not edit anything.

### 2. Draft the contract via interactive Q&A

Use **AskUserQuestion** to collect each field. Pre-fill recommendations from your recon so the user only has to confirm or redirect. Ask in this order:

1. **Slug** — propose a kebab-case slug; let user confirm or rename.
2. **Objective** — one sentence. If the rough idea is already crisp, just confirm. If vague, propose 2–3 sharper rewrites.
3. **Non-goals** — propose 2–4 things explicitly out of scope based on the recon (e.g. "don't change CI", "don't refactor src/"). Multi-select with edit.
4. **Definition of Done** — propose 3–6 measurable criteria. Mix qualitative ("no test files contain `jest.`") with quantitative ("≥20% test runtime improvement"). Multi-select with edit. **DoD is what the judge checks** — be specific, no fluff.
5. **Validator command** — propose the most relevant command(s) you found in recon (e.g. `pnpm test && pnpm lint`). Confirm or override.
6. **Checkpoint cadence** — propose default (`every 5 file edits OR every 20 minutes`). Confirm or override.
7. **Max rejections** — default 5. Confirm or override.
8. **Judge mode** — default `subagent`. Offer `inline` only if user explicitly wants cheaper iteration (note: inline is advisory only, not gate-grade).
9. **Wakeup seconds** — only ask if validator is unusually fast or slow. Otherwise let goal skill pick cache-aware default.

### 3. Write the contract file

Write `.claude/goals/<slug>/contract.md` with frontmatter from the answers above, plus a body containing:

- `## Context` — 1–3 paragraphs grounding the work
- `## Files to know` — bullet list of relevant paths from recon
- `## Constraints` — anything the agent must NOT do that isn't already a non-goal
- `## Anti-placeholder rule` — copy this verbatim:

  > DO NOT stub, mock, skip, or `.todo` work to make the validator pass. If something cannot be done, surface it in the next checkpoint and stop. Skipped work is an automatic judge rejection.

After writing the contract, capture the validator baseline:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/gk.py" baseline <slug>
```

This runs the validator once and records the pre-existing result (and failing paths, if any) to `baseline.json`; `gk activate` merges it into the goal's state so the judge can subtract pre-existing failures from the goal's account. If the naive failing-path extraction missed paths you can see in the output, re-run with `--paths a,b,c` to record them explicitly.

### 4. Confirm and (optionally) activate

After writing, show the user the contract file path and the rendered frontmatter. Ask one final question via AskUserQuestion: "Activate this goal now?" with options:

- **Yes, start now (Recommended)** — hand off to the goal skill with the slug to begin execution.
- **No, review first** — print path and stop. User can `/goal "<objective>"` later to start.

If the user chose to start, immediately re-enter the **goal** skill set-mode flow at step 3 (skip prep since contract now exists).

## Hard rules

- **No silent defaults on DoD.** Every DoD criterion must be confirmed by the user. The DoD is the judge's grading rubric — sloppiness here breaks everything downstream.
- **Validator must be a real command** that can be run in this repo right now. Don't propose `make test` if there's no Makefile. Run it once during prep to confirm it executes (not necessarily that it passes — just that it runs).
- **Capture the baseline result** with `gk baseline <slug>` (see step 3). The judge uses it to distinguish goal-caused validator failures from pre-existing failures on files the goal never touched. **This is the mechanism that lets a goal proceed when the validator is partly polluted by pre-existing debt** — without it, every checkpoint fails for reasons unrelated to the work.
- **Slug uniqueness:** if `.claude/goals/<slug>/` already exists, ask the user whether to overwrite or pick a new slug. Never silently overwrite.
- **Don't write the contract until all questions are answered.** Partial contracts are worse than no contract.
