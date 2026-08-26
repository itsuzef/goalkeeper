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

    r = gk(proj, "verdict", "my-goal", "reject", stdin=REJECT_RESPONSE)
    t.check("reject exits 0 with RETRY", "RETRY" in r.stdout)
    state = read_json(goals / "my-goal" / "state.json")
    t.check("rejection_count incremented", state["rejection_count"] == 1)
    log = (goals / "my-goal" / "log.md").read_text()
    t.check("fix-list copied verbatim to log", "Create the marker file" in log)

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

    gk(proj, "verdict", "hard-goal", "reject", stdin=REJECT_RESPONSE)
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

    r = gk(proj, "verdict", "link-one", "approve", stdin=APPROVE_RESPONSE)
    t.check("approve in chain prints NEXT: link-two", "NEXT: link-two" in r.stdout)
    chain = read_json(goals / "chain.json")
    t.check("cursor advanced + link_approval recorded",
            chain["cursor"] == 1 and chain["link_approvals"][0]["slug"] == "link-one")
    t.check("link-one marked done",
            read_json(goals / "link-one" / "state.json")["status"] == "done")
    active = read_json(goals / "active.json")
    t.check("active.json moved to link-two", active["slug"] == "link-two")

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

    gk(proj, "verdict", "guarded", "approve", stdin=APPROVE_RESPONSE)
    r = hook(proj, "Edit", str(goals / "guarded" / "log.md"))
    t.check("allows log.md again after goal done", r.returncode == 0)
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
