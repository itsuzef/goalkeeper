#!/usr/bin/env python3
"""End-to-end suite for scripts/gk.py — the goalkeeper state mechanic.

Where test-lifecycle.py asserts the canonical state SHAPES, this suite
asserts the MECHANIC: it drives gk as a subprocess through real lifecycles
in throwaway git repos and checks every transition's on-disk result and
exit code, including the PreToolUse hook-guard.

Usage:
  python3 scripts/test-gk.py [-v]

Exit code 0 on all-pass, 1 on any failure. Requires: stdlib + git.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

GK = Path(__file__).resolve().parent / "gk.py"


class Test:
    def __init__(self, name: str, verbose: bool = False):
        self.name = name
        self.verbose = verbose
        self.checks: list[tuple[str, bool]] = []

    def check(self, desc: str, ok: bool) -> None:
        self.checks.append((desc, ok))
        if self.verbose:
            print(f"    [{'PASS' if ok else 'FAIL'}] {desc}")

    def report(self) -> tuple[int, int]:
        passes = sum(1 for _, ok in self.checks if ok)
        fails = len(self.checks) - passes
        print(f"  [{'OK' if fails == 0 else 'FAIL'}]  {self.name}  "
              f"({passes}/{len(self.checks)})")
        for desc, ok in self.checks:
            if not ok:
                print(f"      FAILED: {desc}")
        return passes, fails


def gk(project: Path, *args: str, stdin: str = ""):
    """Run gk in the project dir; return CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(GK), *args],
        cwd=str(project), input=stdin, capture_output=True, text=True,
        timeout=60,
    )


def mint(proj: Path, slug: str, *extra: str):
    """Mint the single-use judge token a provenance-v1 verdict consumes."""
    return gk(proj, "judge-brief", slug, *extra)


def make_project(tmp: Path, name: str) -> Path:
    proj = tmp / name
    (proj / ".claude" / "goals").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(proj)], check=True,
                   capture_output=True)
    (proj / "README.md").write_text("test project\n")
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(proj), "-c", "user.email=t@t", "-c",
                    "user.name=t", "commit", "-qm", "init"], check=True,
                   capture_output=True)
    return proj


def write_contract(proj: Path, slug: str, validator_cmd: str,
                   max_rejections: int = 2) -> None:
    d = proj / ".claude" / "goals" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "contract.md").write_text(f"""---
slug: {slug}
objective: Test objective for {slug} that is long enough to matter.
non_goals:
  - Do not touch CI
definition_of_done:
  - The marker file exists
validator:
  command: {validator_cmd}
  success: exit_zero
  timeout_seconds: 30
max_rejections: {max_rejections}
judge_mode: subagent
---

## Context

Test contract.
""")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


REJECT_RESPONSE = """VERDICT: reject

REASONS:
- NOT MET: marker file missing per src/thing.py:1

FIX_LIST:
- Create the marker file

NOTES:
none
"""

APPROVE_RESPONSE = """VERDICT: approve

REASONS:
- MET marker.txt:1 — marker file exists
- Non-goal violations: NONE
- Anti-placeholder check: CLEAN
"""


def test_standalone_lifecycle(tmp: Path, v: bool) -> Test:
    t = Test("standalone: activate → checkpoint → validate → reject → approve", v)
    proj = make_project(tmp, "standalone")
    goals = proj / ".claude" / "goals"
    write_contract(proj, "my-goal", "test -f marker.txt")

    r = gk(proj, "activate", "my-goal")
    t.check("activate exits 0", r.returncode == 0)
    state = read_json(goals / "my-goal" / "state.json")
    t.check("state active, rejection_count 0",
            state["status"] == "active" and state["rejection_count"] == 0)
    t.check("git baseline captured", bool(state["started_at_commit"]))
    active = read_json(goals / "active.json")
    t.check("active.json points at slug", active["slug"] == "my-goal")
    t.check("chain field omitted for standalone", "chain" not in active)
    log = (goals / "my-goal" / "log.md").read_text()
    t.check("activation logged", "— activated" in log)

    r = gk(proj, "activate", "other")
    t.check("second activate refused while active", r.returncode != 0)

    r = gk(proj, "checkpoint", "my-goal", "--message", "did a thing")
    t.check("checkpoint exits 0", r.returncode == 0)
    state = read_json(goals / "my-goal" / "state.json")
    t.check("last_checkpoint_at set", state["last_checkpoint_at"] is not None)

    r = gk(proj, "validate", "my-goal")
    t.check("validator fails (marker missing) → exit 1", r.returncode == 1)
    state = read_json(goals / "my-goal" / "state.json")
    t.check("last_validator_result records fail",
            str(state["last_validator_result"]).startswith("fail"))

    (proj / "marker.txt").write_text("done\n")
    r = gk(proj, "validate", "my-goal")
    t.check("validator passes → exit 0", r.returncode == 0)
    t.check("VALIDATOR: pass printed", "VALIDATOR: pass" in r.stdout)

    mint(proj, "my-goal")
    r = gk(proj, "verdict", "my-goal", "reject", stdin=REJECT_RESPONSE)
    t.check("reject exits 0 with RETRY", "RETRY" in r.stdout)
    state = read_json(goals / "my-goal" / "state.json")
    t.check("rejection_count incremented", state["rejection_count"] == 1)
    log = (goals / "my-goal" / "log.md").read_text()
    t.check("fix-list copied verbatim to log", "Create the marker file" in log)

    mint(proj, "my-goal")
    r = gk(proj, "verdict", "my-goal", "approve", stdin=APPROVE_RESPONSE)
    t.check("approve prints DONE", "DONE" in r.stdout)
    state = read_json(goals / "my-goal" / "state.json")
    t.check("status done, verdict approve, approved_at set",
            state["status"] == "done" and state["last_judge_verdict"] == "approve"
            and bool(state.get("approved_at")))
    active = read_json(goals / "active.json")
    t.check("active.json terminal with ended_reason done",
            active["slug"] is None and active["ended_reason"] == "done"
            and active["previous_slug"] == "my-goal")
    return t


def test_needs_human_and_resume(tmp: Path, v: bool) -> Test:
    t = Test("max rejections → needs_human → resume gating", v)
    proj = make_project(tmp, "needshuman")
    goals = proj / ".claude" / "goals"
    write_contract(proj, "hard-goal", "true", max_rejections=2)
    gk(proj, "activate", "hard-goal")

    mint(proj, "hard-goal")
    gk(proj, "verdict", "hard-goal", "reject", stdin=REJECT_RESPONSE)
    mint(proj, "hard-goal")
    r = gk(proj, "verdict", "hard-goal", "reject", stdin=REJECT_RESPONSE)
    t.check("second reject prints NEEDS_HUMAN", "NEEDS_HUMAN" in r.stdout)
    state = read_json(goals / "hard-goal" / "state.json")
    t.check("status needs_human + needs_human_at set",
            state["status"] == "needs_human" and bool(state.get("needs_human_at")))
    active = read_json(goals / "active.json")
    t.check("active.json stays active-shape on needs_human",
            active["slug"] == "hard-goal")

    r = gk(proj, "resume")
    t.check("bare resume from needs_human refused", r.returncode != 0)
    r = gk(proj, "resume", "--reset-rejections")
    t.check("resume --reset-rejections exits 0", r.returncode == 0)
    state = read_json(goals / "hard-goal" / "state.json")
    t.check("active again with counter reset",
            state["status"] == "active" and state["rejection_count"] == 0)

    r = gk(proj, "pause")
    t.check("pause exits 0", r.returncode == 0)
    state = read_json(goals / "hard-goal" / "state.json")
    t.check("paused with paused_at", state["status"] == "paused"
            and bool(state.get("paused_at")))
    r = gk(proj, "resume", "--keep-count")
    t.check("resume from paused works", r.returncode == 0)
    return t


def test_chain_lifecycle(tmp: Path, v: bool) -> Test:
    t = Test("chain: start → approve advances → complete", v)
    proj = make_project(tmp, "chain")
    goals = proj / ".claude" / "goals"
    write_contract(proj, "link-one", "true")
    write_contract(proj, "link-two", "true")
    chain_file = proj / "chain.md"
    chain_file.write_text("---\nname: test-chain\n---\n\n1. link-one\n2. link-two\n")

    r = gk(proj, "chain-start", str(chain_file))
    t.check("chain-start exits 0, NEXT: link-one",
            r.returncode == 0 and "NEXT: link-one" in r.stdout)
    chain = read_json(goals / "chain.json")
    t.check("chain.json cursor 0, active",
            chain["cursor"] == 0 and chain["status"] == "active")
    active = read_json(goals / "active.json")
    t.check("first link active with chain field",
            active["slug"] == "link-one" and active["chain"] == "test-chain")
    state = read_json(goals / "link-one" / "state.json")
    t.check("chain_step 1 on first link", state.get("chain_step") == 1)

    mint(proj, "link-one")
    r = gk(proj, "verdict", "link-one", "approve", stdin=APPROVE_RESPONSE)
    t.check("approve in chain prints NEXT: link-two", "NEXT: link-two" in r.stdout)
    chain = read_json(goals / "chain.json")
    t.check("cursor advanced + link_approval recorded",
            chain["cursor"] == 1 and chain["link_approvals"][0]["slug"] == "link-one")
    t.check("link-one marked done",
            read_json(goals / "link-one" / "state.json")["status"] == "done")
    active = read_json(goals / "active.json")
    t.check("active.json moved to link-two", active["slug"] == "link-two")

    mint(proj, "link-two")
    r = gk(proj, "verdict", "link-two", "approve", stdin=APPROVE_RESPONSE)
    t.check("final approve prints CHAIN_COMPLETE", "CHAIN_COMPLETE" in r.stdout)
    chain = read_json(goals / "chain.json")
    t.check("chain done with completed_at",
            chain["status"] == "done" and bool(chain["completed_at"]))
    active = read_json(goals / "active.json")
    t.check("terminal active.json: chain_completed + previous_chain",
            active["slug"] is None and active["ended_reason"] == "chain_completed"
            and active["previous_chain"] == "test-chain")
    return t


def test_clear_aborts_chain(tmp: Path, v: bool) -> Test:
    t = Test("clear: archives goal, aborts in-flight chain", v)
    proj = make_project(tmp, "clearchain")
    goals = proj / ".claude" / "goals"
    write_contract(proj, "link-a", "true")
    write_contract(proj, "link-b", "true")
    cf = proj / "c.md"
    cf.write_text("- link-a\n- link-b\n")
    gk(proj, "chain-start", str(cf))

    r = gk(proj, "clear")
    t.check("clear without --yes refused", r.returncode != 0)
    r = gk(proj, "clear", "--yes")
    t.check("clear --yes exits 0", r.returncode == 0)
    chain = read_json(goals / "chain.json")
    t.check("chain aborted", chain["status"] == "aborted")
    active = read_json(goals / "active.json")
    t.check("terminal ended_reason cleared + previous_chain set",
            active["ended_reason"] == "cleared"
            and active.get("previous_chain") == "c")
    t.check("goal dir moved to _archive",
            not (goals / "link-a").exists()
            and any((goals / "_archive").glob("link-a-*")))
    t.check("unreached link-b contract untouched",
            (goals / "link-b" / "contract.md").is_file())
    return t


def test_baseline_capture(tmp: Path, v: bool) -> Test:
    t = Test("baseline: pre-activation validator result merged into state", v)
    proj = make_project(tmp, "baseline")
    goals = proj / ".claude" / "goals"
    write_contract(proj, "base-goal", "test -f does-not-exist.txt")  # always fails
    r = gk(proj, "baseline", "base-goal")
    t.check("baseline exits 0", r.returncode == 0)
    b = read_json(goals / "base-goal" / "baseline.json")
    t.check("baseline recorded fail", b["result"] == "fail")
    gk(proj, "activate", "base-goal")
    state = read_json(goals / "base-goal" / "state.json")
    t.check("validator_baseline_result merged on activate",
            state.get("validator_baseline_result") == "fail")
    return t


def test_judge_brief(tmp: Path, v: bool) -> Test:
    t = Test("judge-brief: assembled prompt has all sections", v)
    proj = make_project(tmp, "brief")
    write_contract(proj, "brief-goal", "true")
    gk(proj, "activate", "brief-goal")
    (proj / "newfile.py").write_text("print('hello')\n")
    (proj / "README.md").write_text("test project\nchanged\n")
    r = gk(proj, "judge-brief", "brief-goal")
    t.check("exits 0", r.returncode == 0)
    out = r.stdout
    for section in ("# Contract", "# Progress log", "# Diff scope",
                    "# Files modified or added", "# Diff (excerpt)",
                    "VERDICT: approve", "file:line"):
        t.check(f"contains '{section}'", section in out)
    t.check("modified file listed", "README.md" in out)
    t.check("untracked file listed", "newfile.py" in out)
    t.check("contract objective present", "Test objective for brief-goal" in out)
    return t


def test_compact_log(tmp: Path, v: bool) -> Test:
    t = Test("log --compact: keeps lifecycle blocks, trims old checkpoints", v)
    proj = make_project(tmp, "compact")
    goals = proj / ".claude" / "goals"
    write_contract(proj, "log-goal", "true")
    gk(proj, "activate", "log-goal")
    for i in range(10):
        gk(proj, "checkpoint", "log-goal", "--message", f"checkpoint number {i}")
    mint(proj, "log-goal")
    gk(proj, "verdict", "log-goal", "reject", stdin=REJECT_RESPONSE)
    r = gk(proj, "log", "log-goal", "--compact", "--checkpoints", "3")
    out = r.stdout
    t.check("activation kept", "— activated" in out)
    t.check("judge block kept", "judge rejected" in out)
    t.check("recent checkpoint kept", "checkpoint number 9" in out)
    t.check("old checkpoint dropped", "checkpoint number 0" not in out)
    t.check("omission marker present", "[compacted:" in out)
    full = gk(proj, "log", "log-goal").stdout
    t.check("full log still complete on disk", "checkpoint number 0" in full)
    return t


def test_doctor(tmp: Path, v: bool) -> Test:
    t = Test("doctor: detects and repairs stalled chain advance", v)
    proj = make_project(tmp, "doctor")
    goals = proj / ".claude" / "goals"
    write_contract(proj, "doc-one", "true")
    write_contract(proj, "doc-two", "true")
    cf = proj / "c.md"
    cf.write_text("- doc-one\n- doc-two\n")
    gk(proj, "chain-start", str(cf))
    # Simulate symptom C: goal approved+done but cursor never advanced.
    state = read_json(goals / "doc-one" / "state.json")
    state.update({"status": "done", "last_judge_verdict": "approve",
                  "approved_at": "2026-01-01T00:00:00Z"})
    (goals / "doc-one" / "state.json").write_text(json.dumps(state))
    r = gk(proj, "doctor")
    t.check("doctor detects problem (exit 1)", r.returncode == 1
            and "PROBLEM" in r.stdout)
    r = gk(proj, "doctor", "--fix")
    t.check("doctor --fix exits 0", r.returncode == 0)
    chain = read_json(goals / "chain.json")
    t.check("cursor advanced to 1", chain["cursor"] == 1)
    active = read_json(goals / "active.json")
    t.check("doc-two activated", active["slug"] == "doc-two")
    r = gk(proj, "doctor")
    t.check("clean after repair", r.returncode == 0 and "OK" in r.stdout)
    return t


PROCEED_RESPONSE = """VERDICT: proceed

REASONING:
The prior goal shipped the marker file; the mission needs docs next.

NEXT_OBJECTIVE: Write the user documentation for the marker feature.
"""

DONE_RESPONSE = """VERDICT: done

REASONING:
Both success-condition items are demonstrated by the prior goals.

DONE_EVIDENCE:
- Marker exists: marker.txt shipped by my-goal
"""

ESCALATE_RESPONSE = """VERDICT: escalate

REASONING:
The charter's success condition references a metric no goal produced.

ESCALATION:
Need the user to define how conversion-rate is measured.
"""

MISSION_MD = """# Mission: test-mission

## Objective

Ship the marker feature end to end.

## Success condition

marker.txt exists and is documented.

## Legal next-goal shapes

- implement: create the marker
- document: write the docs
"""


def make_mission_project(tmp: Path, name: str) -> Path:
    """Project with a completed goal and a mission charter."""
    proj = make_project(tmp, name)
    write_contract(proj, "my-goal", "test -f marker.txt")
    gk(proj, "activate", "my-goal")
    (proj / "marker.txt").write_text("done\n")
    mint(proj, "my-goal")
    gk(proj, "verdict", "my-goal", "approve", stdin=APPROVE_RESPONSE)
    (proj / ".claude" / "mission.md").write_text(MISSION_MD)
    return proj


def test_mission_lifecycle(tmp: Path, v: bool) -> Test:
    t = Test("mission: init → brief → proceed → done, with guards", v)
    proj = make_project(tmp, "mission")
    claude = proj / ".claude"

    r = gk(proj, "mission-init")
    t.check("init refused without mission.md", r.returncode != 0)

    (claude / "mission.md").write_text(MISSION_MD)
    write_contract(proj, "my-goal", "test -f marker.txt")
    gk(proj, "activate", "my-goal")
    r = gk(proj, "mission-init")
    t.check("init refused while a goal is in flight", r.returncode != 0)

    (proj / "marker.txt").write_text("done\n")
    mint(proj, "my-goal")
    gk(proj, "verdict", "my-goal", "approve", stdin=APPROVE_RESPONSE)
    r = gk(proj, "mission-init")
    t.check("init succeeds after goal done", r.returncode == 0)
    mission = read_json(claude / "mission.json")
    t.check("mission.json shape: name from charter, active, empty lists",
            mission["name"] == "test-mission" and mission["status"] == "active"
            and mission["goals_completed"] == []
            and mission["supervisor_verdicts"] == [])
    r = gk(proj, "mission-init")
    t.check("re-init is a no-op", r.returncode == 0 and "already" in r.stdout)

    r = gk(proj, "mission-brief")
    t.check("brief exits 0", r.returncode == 0)
    out = r.stdout
    for section in ("# Mission charter", "# Prior goal: my-goal",
                    "# Repo state", "VERDICT: proceed", "NEXT_OBJECTIVE"):
        t.check(f"brief contains '{section}'", section in out)
    t.check("brief carries prior goal's compacted log", "— activated" in out)

    r = gk(proj, "mission-verdict", "proceed", stdin=PROCEED_RESPONSE)
    t.check("proceed prints PROCEED + objective",
            "PROCEED" in r.stdout and "user documentation" in r.stdout)
    mission = read_json(claude / "mission.json")
    t.check("verdict + goals_completed recorded",
            mission["supervisor_verdicts"][-1]["verdict"] == "proceed"
            and mission["goals_completed"][0]["slug"] == "my-goal"
            and mission["goals_completed"][0]["result"] == "approved")
    t.check("mission-log entry appended",
            "supervisor verdict: proceed" in
            (claude / "mission-log.md").read_text())
    t.check("prior goal's log notes the verdict",
            "supervisor verdict" in
            (claude / "goals" / "my-goal" / "log.md").read_text())

    r = gk(proj, "mission-verdict", "proceed", stdin=PROCEED_RESPONSE)
    t.check("second verdict on same prior goal refused", r.returncode != 0)

    # complete a second goal, then close the mission
    write_contract(proj, "doc-goal", "true")
    gk(proj, "activate", "doc-goal")
    mint(proj, "doc-goal")
    gk(proj, "verdict", "doc-goal", "approve", stdin=APPROVE_RESPONSE)
    r = gk(proj, "mission-verdict", "done", stdin=DONE_RESPONSE)
    t.check("done prints DONE", "DONE" in r.stdout)
    mission = read_json(claude / "mission.json")
    t.check("mission done with completed_at",
            mission["status"] == "done" and bool(mission.get("completed_at")))
    snapshot = (claude / "mission-completed.md").read_text()
    t.check("mission-completed.md snapshot has charter + evidence",
            "test-mission" in snapshot and "Marker exists" in snapshot)
    r = gk(proj, "mission-verdict", "escalate", stdin=ESCALATE_RESPONSE)
    t.check("verdicts refused on a non-active mission", r.returncode != 0)
    return t


def test_mission_fresh_init(tmp: Path, v: bool) -> Test:
    t = Test("mission: init on a fresh project (no .claude/goals yet)", v)
    proj = tmp / "mission-fresh"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "mission.md").write_text(MISSION_MD)
    r = gk(proj, "mission-init")
    t.check("init succeeds without pre-existing .claude/goals",
            r.returncode == 0)
    goals = proj / ".claude" / "goals"
    t.check(".claude/goals created", goals.is_dir())
    t.check("goals .gitignore seeded", (goals / ".gitignore").is_file())
    mission = read_json(proj / ".claude" / "mission.json")
    t.check("mission.json initialized",
            mission is not None and mission["status"] == "active")
    r = gk(proj, "mission-brief")
    t.check("brief works on the fresh mission (first-invocation framing)",
            r.returncode == 0 and "first supervisor invocation" in r.stdout)
    return t


def test_mission_escalate_and_recovery(tmp: Path, v: bool) -> Test:
    t = Test("mission: escalate → resume → re-verdict on same prior goal", v)
    proj = make_mission_project(tmp, "mission-esc")
    claude = proj / ".claude"
    gk(proj, "mission-init")
    r = gk(proj, "mission-verdict", "proceed", stdin="VERDICT: proceed\n")
    t.check("proceed without NEXT_OBJECTIVE refused", r.returncode != 0)
    r = gk(proj, "mission-resume")
    t.check("resume refused while mission active", r.returncode != 0)

    r = gk(proj, "mission-verdict", "escalate", stdin=ESCALATE_RESPONSE)
    t.check("escalate prints ESCALATE + required input",
            "ESCALATE" in r.stdout and "conversion-rate" in r.stdout)
    mission = read_json(claude / "mission.json")
    t.check("mission status escalated", mission["status"] == "escalated")

    r = gk(proj, "mission-verdict", "proceed", stdin=PROCEED_RESPONSE)
    t.check("verdict refused while escalated (un-resumed)", r.returncode != 0)
    r = gk(proj, "mission-brief")
    t.check("brief still works while escalated", r.returncode == 0)

    r = gk(proj, "mission-resume", "--note", "defined conversion-rate in charter")
    t.check("mission-resume exits 0", r.returncode == 0)
    mission = read_json(claude / "mission.json")
    t.check("status back to active", mission["status"] == "active")
    log = (claude / "mission-log.md").read_text()
    t.check("resolution logged with note",
            "escalation resolved" in log and "defined conversion-rate" in log)

    r = gk(proj, "mission-verdict", "proceed", stdin=PROCEED_RESPONSE)
    t.check("same-prior-goal verdict accepted after resolved escalation",
            r.returncode == 0 and "PROCEED" in r.stdout)
    mission = read_json(claude / "mission.json")
    t.check("both verdicts on record (escalate then proceed)",
            [x["verdict"] for x in mission["supervisor_verdicts"]]
            == ["escalate", "proceed"])
    r = gk(proj, "mission-verdict", "proceed", stdin=PROCEED_RESPONSE)
    t.check("one-per-goal-completion guard still holds after proceed",
            r.returncode != 0)
    return t


def test_mission_hook_guard(tmp: Path, v: bool) -> Test:
    t = Test("hook-guard: mission files blocked while live, charter never", v)
    proj = make_mission_project(tmp, "mission-hook")
    claude = proj / ".claude"

    r = hook(proj, "Edit", str(claude / "mission-log.md"))
    t.check("allows mission-log.md before init", r.returncode == 0)
    gk(proj, "mission-init")
    for fname in ("mission.json", "mission-log.md", "mission-completed.md"):
        r = hook(proj, "Edit", str(claude / fname))
        t.check(f"blocks {fname} while mission active", r.returncode == 2)
    r = hook(proj, "Edit", str(claude / "mission.md"))
    t.check("mission.md (user charter) never blocked", r.returncode == 0)
    r = hook(proj, "Edit", str(claude / "settings.json"))
    t.check("other .claude files untouched", r.returncode == 0)

    gk(proj, "mission-verdict", "escalate", stdin=ESCALATE_RESPONSE)
    r = hook(proj, "Edit", str(claude / "mission.json"))
    t.check("still blocked while escalated", r.returncode == 2)

    mission = read_json(claude / "mission.json")
    mission["status"] = "done"
    (claude / "mission.json").write_text(json.dumps(mission))
    r = hook(proj, "Edit", str(claude / "mission.json"))
    t.check("allowed after mission done", r.returncode == 0)
    return t


def hook(proj: Path, tool: str, path: str):
    payload = json.dumps({"tool_name": tool, "tool_input": {"file_path": path}})
    return gk(proj, "hook-guard", stdin=payload)


def test_hook_guard(tmp: Path, v: bool) -> Test:
    t = Test("hook-guard: blocks managed files of active goal, fails open", v)
    proj = make_project(tmp, "hooks")
    goals = proj / ".claude" / "goals"
    write_contract(proj, "guarded", "true")

    r = hook(proj, "Edit", str(goals / "guarded" / "contract.md"))
    t.check("allows contract edit when goal not active", r.returncode == 0)

    gk(proj, "activate", "guarded")
    for fname in ("contract.md", "log.md", "state.json"):
        r = hook(proj, "Edit", str(goals / "guarded" / fname))
        t.check(f"blocks {fname} while active (exit 2)", r.returncode == 2)
    r = hook(proj, "Write", str(goals / "active.json"))
    t.check("blocks active.json", r.returncode == 2)
    r = hook(proj, "Write", str(goals / "chain.json"))
    t.check("blocks chain.json", r.returncode == 2)
    t.check("block message names gk", "gk" in
            hook(proj, "Edit", str(goals / "guarded" / "contract.md")).stderr)

    r = hook(proj, "Edit", str(goals / "other-goal" / "contract.md"))
    t.check("allows a NON-active goal's contract (prep flow)", r.returncode == 0)
    r = hook(proj, "Edit", str(proj / "src" / "main.py"))
    t.check("allows ordinary project files", r.returncode == 0)
    r = hook(proj, "Edit", str(goals / "_archive" / "old-20260101" / "log.md"))
    t.check("allows _archive files", r.returncode == 0)
    r = gk(proj, "hook-guard", stdin="not json at all {")
    t.check("fails open on garbage stdin", r.returncode == 0)

    mint(proj, "guarded")
    gk(proj, "verdict", "guarded", "approve", stdin=APPROVE_RESPONSE)
    r = hook(proj, "Edit", str(goals / "guarded" / "log.md"))
    t.check("allows log.md again after goal done", r.returncode == 0)
    return t



def test_judge_provenance(tmp: Path, v: bool) -> Test:
    t = Test("judge provenance: mint/consume, mode persistence, legacy path", v)
    proj = make_project(tmp, "provenance")
    goals = proj / ".claude" / "goals"
    write_contract(proj, "prov-goal", "true", max_rejections=5)
    gk(proj, "activate", "prov-goal")
    state = read_json(goals / "prov-goal" / "state.json")
    t.check("activation stamps provenance_version 1",
            state.get("provenance_version") == 1)
    t.check("activation records executor observables",
            isinstance(state.get("executor"), dict)
            and state["executor"].get("recorded_at"))
    t.check("judge_verdicts starts as empty list",
            state.get("judge_verdicts") == [])

    r = gk(proj, "verdict", "prov-goal", "reject", stdin=REJECT_RESPONSE)
    t.check("verdict without a mint is refused",
            r.returncode != 0 and "judge token" in (r.stderr + r.stdout))
    state = read_json(goals / "prov-goal" / "state.json")
    t.check("refused verdict leaves no record",
            state["judge_verdicts"] == [] and state["rejection_count"] == 0)

    r = mint(proj, "prov-goal")
    t.check("judge-brief mints and says so on stderr",
            r.returncode == 0 and "token minted" in r.stderr)
    tok = read_json(goals / "prov-goal" / "judge-token.json")
    t.check("token unused, mode defaults to contract (subagent)",
            tok["used"] is False and tok["mode"] == "subagent"
            and tok["token_id"])

    r = hook(proj, "Edit", str(goals / "prov-goal" / "judge-token.json"))
    t.check("hook-guard blocks direct edit of the token file",
            r.returncode == 2)

    r = gk(proj, "verdict", "prov-goal", "reject", stdin=REJECT_RESPONSE)
    t.check("reject with token accepted", "RETRY" in r.stdout)
    state = read_json(goals / "prov-goal" / "state.json")
    t.check("verdict recorded append-only with executed mode",
            len(state["judge_verdicts"]) == 1
            and state["judge_verdicts"][0]["verdict"] == "reject"
            and state["judge_verdicts"][0]["mode"] == "subagent"
            and state["last_judge_mode"] == "subagent")
    tok = read_json(goals / "prov-goal" / "judge-token.json")
    t.check("token consumed (used + used_at)",
            tok["used"] is True and tok.get("used_at"))

    r = gk(proj, "verdict", "prov-goal", "reject", stdin=REJECT_RESPONSE)
    t.check("a consumed token cannot be reused", r.returncode != 0)

    mint(proj, "prov-goal", "--mode", "inline")
    r = gk(proj, "verdict", "prov-goal", "approve", stdin=APPROVE_RESPONSE)
    t.check("inline token refused for gate-quality approve",
            r.returncode != 0 and "inline" in (r.stderr + r.stdout))
    r = gk(proj, "verdict", "prov-goal", "reject", stdin=REJECT_RESPONSE)
    t.check("inline reject allowed and recorded as inline",
            "RETRY" in r.stdout
            and read_json(goals / "prov-goal" / "state.json")
            ["judge_verdicts"][-1]["mode"] == "inline")

    mint(proj, "prov-goal")
    r = gk(proj, "verdict", "prov-goal", "approve", stdin=APPROVE_RESPONSE)
    t.check("fresh subagent mint approves to DONE", "DONE" in r.stdout)
    state = read_json(goals / "prov-goal" / "state.json")
    t.check("history holds every accepted verdict (3), none refused",
            len(state["judge_verdicts"]) == 3
            and [e["verdict"] for e in state["judge_verdicts"]]
            == ["reject", "reject", "approve"])

    # Pre-provenance goal (activated before the upgrade): no token required.
    write_contract(proj, "legacy-goal", "true")
    gk(proj, "activate", "legacy-goal")
    sp = goals / "legacy-goal" / "state.json"
    legacy = read_json(sp)
    for k in ("provenance_version", "executor", "judge_verdicts"):
        legacy.pop(k, None)
    sp.write_text(json.dumps(legacy, indent=2))
    r = gk(proj, "verdict", "legacy-goal", "approve", stdin=APPROVE_RESPONSE)
    t.check("legacy goal approves without a token", "DONE" in r.stdout)
    legacy = read_json(sp)
    t.check("legacy verdict recorded and flagged legacy",
            legacy["judge_verdicts"][-1].get("legacy") is True
            and legacy["judge_verdicts"][-1]["mode"] is None)
    return t


def test_verdict_receipt(tmp: Path, v: bool) -> Test:
    t = Test("verdict receipt: minted on verdict, exportable, hook-guarded", v)
    proj = make_project(tmp, "receipt")
    goals = proj / ".claude" / "goals"
    write_contract(proj, "rcpt-goal", "true", max_rejections=5)
    gk(proj, "activate", "rcpt-goal")

    r = gk(proj, "receipt", "rcpt-goal")
    t.check("no receipt before any verdict",
            r.returncode != 0 and "no receipt" in (r.stderr + r.stdout))

    mint(proj, "rcpt-goal")
    gk(proj, "verdict", "rcpt-goal", "reject", stdin=REJECT_RESPONSE)
    rcpt = read_json(goals / "rcpt-goal" / "receipt.json")
    t.check("reject mints a receipt", rcpt is not None)
    t.check("reject receipt: decision, mode, not gate-quality",
            rcpt["decision"] == "reject" and rcpt["mode"] == "subagent"
            and rcpt["gate_quality"] is False
            and rcpt["rejection_count"] == 1)
    state = read_json(goals / "rcpt-goal" / "state.json")
    t.check("receipt token id matches the consumed token in history",
            rcpt["token"]["token_id"] == state["judge_verdicts"][0]["token_id"]
            and rcpt["verdict_index"] == 0)

    r = hook(proj, "Edit", str(goals / "rcpt-goal" / "receipt.json"))
    t.check("hook-guard blocks direct edit of the receipt",
            r.returncode == 2)

    mint(proj, "rcpt-goal")
    gk(proj, "verdict", "rcpt-goal", "approve", stdin=APPROVE_RESPONSE)
    rcpt = read_json(goals / "rcpt-goal" / "receipt.json")
    t.check("approve overwrites with a gate-quality receipt",
            rcpt["decision"] == "approve" and rcpt["gate_quality"] is True
            and rcpt["verdict_index"] == 1)
    t.check("receipt binds the repo commit judged",
            isinstance(rcpt["repo"]["head"], str)
            and len(rcpt["repo"]["head"]) == 40
            and rcpt["repo"]["started_at_commit"])
    import hashlib
    contract_hash = hashlib.sha256(
        (goals / "rcpt-goal" / "contract.md").read_bytes()).hexdigest()
    t.check("receipt binds the contract hash",
            rcpt["contract_sha256"] == contract_hash)
    t.check("receipt records executor observables",
            isinstance(rcpt["executor"], dict))

    r = gk(proj, "receipt", "rcpt-goal")
    t.check("gk receipt prints parseable JSON",
            r.returncode == 0
            and json.loads(r.stdout)["decision"] == "approve")

    # Pre-provenance goal: verdicts mint no receipt, and `gk receipt` says why.
    write_contract(proj, "legacy-r", "true")
    gk(proj, "activate", "legacy-r")
    sp = goals / "legacy-r" / "state.json"
    legacy = read_json(sp)
    for k in ("provenance_version", "executor", "judge_verdicts"):
        legacy.pop(k, None)
    sp.write_text(json.dumps(legacy, indent=2))
    gk(proj, "verdict", "legacy-r", "approve", stdin=APPROVE_RESPONSE)
    t.check("legacy verdict mints no receipt",
            not (goals / "legacy-r" / "receipt.json").exists())
    r = gk(proj, "receipt", "legacy-r")
    t.check("gk receipt explains the pre-provenance case",
            r.returncode != 0 and "pre-provenance" in (r.stderr + r.stdout))
    return t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()

    tests = [
        test_standalone_lifecycle,
        test_needs_human_and_resume,
        test_chain_lifecycle,
        test_clear_aborts_chain,
        test_baseline_capture,
        test_judge_brief,
        test_compact_log,
        test_doctor,
        test_hook_guard,
        test_judge_provenance,
        test_verdict_receipt,
        test_mission_lifecycle,
        test_mission_fresh_init,
        test_mission_escalate_and_recovery,
        test_mission_hook_guard,
    ]
    total_pass = total_fail = 0
    print("gk end-to-end suite\n")
    with tempfile.TemporaryDirectory(prefix="gk-test-") as td:
        tmp = Path(td)
        for fn in tests:
            try:
                t = fn(tmp, args.v)
            except Exception as e:  # a crashed test is a failed test
                t = Test(fn.__name__, args.v)
                t.check(f"test crashed: {e!r}", False)
            p, f = t.report()
            total_pass += p
            total_fail += f
    print(f"\n{total_pass} passed, {total_fail} failed")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
