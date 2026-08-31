#!/usr/bin/env python3
"""gk — the goalkeeper state mechanic.

Every state transition goalkeeper makes (activate, checkpoint, validate,
verdict, advance, pause, resume, clear) is executed by this script, not by
prose in a SKILL.md. Skills decide WHAT to do; gk does the writes. This is
the single implementation of the canonical state shapes — if a shape needs
to change, change it here and in scripts/test-gk.py together.

Stdlib only. Python 3.9+. Exit codes: 0 success, 1 refusal/failure,
2 hook-block (hook-guard only).

Usage:
  gk status [--json]
  gk activate <slug> [--chain NAME --chain-step N] [--force]
  gk baseline <slug> [--paths a,b,c]
  gk checkpoint <slug> [--message TEXT]          (message may come from stdin)
  gk validate <slug>
  gk judge-brief <slug> [--executor-summary FILE] [--mode subagent|inline]
                                                 (mints the single-use judge token)
  gk verdict <slug> approve|reject               (judge's structured response on stdin;
                                                  consumes the judge token and mints
                                                  the verdict receipt)
  gk receipt <slug>                              (print the exportable verdict receipt)
  gk advance [--fix]
  gk chain-start <chain-file>
  gk pause
  gk park [slug] --needs TEXT                    (human-gated blocker: record what a
                                                  person must provide, free the slot,
                                                  continue with other work)
  gk resume [slug] [--reset-rejections | --keep-count]
  gk clear --yes
  gk log <slug> [--compact] [--checkpoints N]
  gk doctor [--fix]
  gk mission-init
  gk mission-status
  gk mission-brief
  gk mission-verdict proceed|done|escalate       (supervisor's structured response on stdin)
  gk mission-resume [--note TEXT]                (escalated → active, after the user resolves)
  gk hook-guard                                  (PreToolUse JSON on stdin)

Every transition also emits a kernel audit event, and a park raises a kernel
condition naming the person who must act — best-effort, never blocking. See the
"Kernel emission" section below. Environment:

  OSD_KERNEL_ADAPTER   path to runtime/kernel_adapter.py (else documented
                       defaults; not found = silent no-op)
  GK_KERNEL_EMIT=0     turn emission off entirely
  GK_DECISION_OWNER    actor ref a raised condition is routed to (default
                       actor:chef)
  GK_KERNEL_DOMAIN     kernel domain id (default kernel:shared-control)
  GK_KERNEL_ACTOR      overrides the emitting actor ref (default
                       process:goalkeeper); distinct from GK_ACTOR, which
                       names the executing agent in state.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_EXCLUDES = [
    ":!package-lock.json", ":!yarn.lock", ":!pnpm-lock.yaml",
    ":!Cargo.lock", ":!poetry.lock", ":!go.sum",
    ":!Gemfile.lock", ":!composer.lock",
    ":!dist/**", ":!build/**", ":!out/**", ":!target/**", ":!.next/**",
    ":!**/*.min.js", ":!**/*.min.css",
    ":!coverage/**", ":!.nyc_output/**", ":!test-results/**",
    ":!.vscode/**", ":!.idea/**", ":!.DS_Store",
]

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
DIFF_CAP_CHARS = 150_000
GOALS_GITIGNORE = "*\n!shared/\n!.gitignore\n"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(msg: str, code: int = 1) -> "None":
    print(f"gk: {msg}", file=sys.stderr)
    sys.exit(code)


# ─────────────────────────────────────────────────────────────────────────────
# Filesystem + state primitives
# ─────────────────────────────────────────────────────────────────────────────

def find_goals_dir(create: bool = False) -> Path:
    """Nearest ancestor of cwd containing .claude/goals; optionally create."""
    cur = Path.cwd().resolve()
    for d in [cur, *cur.parents]:
        cand = d / ".claude" / "goals"
        if cand.is_dir():
            return cand
    if not create:
        die("no .claude/goals directory found here or in any parent. "
            "Run from the project, or `gk activate` to create one.")
    # Prefer an existing .claude dir; else project root = cwd.
    for d in [cur, *cur.parents]:
        if (d / ".claude").is_dir():
            root = d
            break
    else:
        root = cur
    goals = root / ".claude" / "goals"
    goals.mkdir(parents=True, exist_ok=True)
    gi = goals / ".gitignore"
    if not gi.exists():
        gi.write_text(GOALS_GITIGNORE)
    return goals


def project_root(goals: Path) -> Path:
    return goals.parent.parent


def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict) -> None:
    """Atomic write: tmp + rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def append_log(goals: Path, slug: str, title: str, body: str = "") -> None:
    log = goals / slug / "log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    entry = f"\n## {now_iso()} — {title}\n"
    if body.strip():
        entry += body.rstrip() + "\n"
    with log.open("a") as f:
        f.write(entry)


def active_info(goals: Path) -> Optional[str]:
    """Return the active slug, or None if active.json missing/terminal."""
    data = read_json(goals / "active.json")
    if not data or not data.get("slug"):
        return None
    return data["slug"]


def load_state(goals: Path, slug: str) -> Optional[dict]:
    return read_json(goals / slug / "state.json")


def save_state(goals: Path, slug: str, state: dict) -> None:
    write_json(goals / slug / "state.json", state)


# ─────────────────────────────────────────────────────────────────────────────
# Contract frontmatter (minimal YAML subset — scalars, one-level maps, lists)
# ─────────────────────────────────────────────────────────────────────────────

def _coerce(val: str):
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    if val in ("true", "false"):
        return val == "true"
    if val in ("null", "~", ""):
        return None
    return val


def parse_frontmatter(text: str):
    """Return (meta_dict, body). Supports the contract-schema subset only."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta: dict = {}
    cur_key = None       # top-level key awaiting nested content
    cur_kind = None      # "map" | "list" | None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            if val.strip():
                meta[key] = _coerce(val)
                cur_key, cur_kind = None, None
            else:
                meta[key] = None  # placeholder until we see nested shape
                cur_key, cur_kind = key, None
        elif cur_key is not None:
            if line.startswith("- "):
                if cur_kind != "list":
                    meta[cur_key], cur_kind = [], "list"
                meta[cur_key].append(_coerce(line[2:]))
            elif ":" in line:
                if cur_kind != "map":
                    meta[cur_key], cur_kind = {}, "map"
                k, _, v = line.partition(":")
                meta[cur_key][k.strip()] = _coerce(v)
    body = "\n".join(lines[end + 1:])
    return meta, body


def load_contract(goals: Path, slug: str):
    path = goals / slug / "contract.md"
    if not path.is_file():
        die(f"no contract at {path}. Run /goal-prep first — the contract IS the spec.")
    meta, body = parse_frontmatter(path.read_text())
    return meta, body, path


# ─────────────────────────────────────────────────────────────────────────────
# Git helpers
# ─────────────────────────────────────────────────────────────────────────────

def _git(root: Path, *args: str, check: bool = False) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None if not check else None
    return r.stdout


def git_baseline(root: Path):
    """(commit_sha_or_None, dirty_paths_list)."""
    head = _git(root, "rev-parse", "HEAD")
    if head is None:
        return None, []
    porcelain = _git(root, "status", "--porcelain") or ""
    dirty = [ln[3:].strip() for ln in porcelain.splitlines() if ln.strip()]
    return head.strip(), dirty


# ─────────────────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────────────────

def run_validator(root: Path, meta: dict):
    """Return (result, output). result: 'pass' | 'fail: <reason>' | 'not_runnable'."""
    validator = meta.get("validator") or {}
    cmd = validator.get("command")
    if not cmd:
        return "not_runnable", "contract has no validator.command"
    # `command: true` is a legitimate no-op validator, and the frontmatter
    # scalar coercion turns it into the Python bool. Hand the shell back the
    # word it was written as rather than a TypeError from subprocess.
    if isinstance(cmd, bool):
        cmd = "true" if cmd else "false"
    elif not isinstance(cmd, str):
        cmd = str(cmd)
    timeout = validator.get("timeout_seconds") or 600
    success = validator.get("success") or "exit_zero"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=int(timeout), cwd=str(root))
    except subprocess.TimeoutExpired:
        return f"fail: validator timed out after {timeout}s", ""
    except OSError as e:
        return "not_runnable", str(e)
    output = (r.stdout or "") + (r.stderr or "")
    if success.startswith("regex:"):
        ok = re.search(success[len("regex:"):], output) is not None
        reason = "output did not match success regex"
    else:
        ok = r.returncode == 0
        reason = f"exit code {r.returncode}"
    return ("pass" if ok else f"fail: {reason}"), output


def tail(text: str, n: int = 40) -> str:
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-n:])


# ─────────────────────────────────────────────────────────────────────────────
# Log compaction
# ─────────────────────────────────────────────────────────────────────────────

def split_log_blocks(log_text: str):
    """Split log.md into (preamble, [(title_line, body), ...])."""
    blocks = []
    cur_title, cur_body = None, []
    preamble = []
    for line in log_text.splitlines():
        if line.startswith("## "):
            if cur_title is not None:
                blocks.append((cur_title, "\n".join(cur_body)))
            cur_title, cur_body = line, []
        elif cur_title is None:
            preamble.append(line)
        else:
            cur_body.append(line)
    if cur_title is not None:
        blocks.append((cur_title, "\n".join(cur_body)))
    return "\n".join(preamble), blocks


ALWAYS_KEEP = ("activated", "judge", "blocked", "paused", "resumed",
               "recovery", "cleared", "done", "supervisor")


def compact_log(log_text: str, keep_checkpoints: int = 5) -> str:
    """Activation + every judge/lifecycle block + last N checkpoints."""
    preamble, blocks = split_log_blocks(log_text)
    keep = [False] * len(blocks)
    checkpoint_idx = []
    for i, (title, _) in enumerate(blocks):
        low = title.lower()
        if any(k in low for k in ALWAYS_KEEP):
            keep[i] = True
        elif "checkpoint" in low or "validator" in low:
            checkpoint_idx.append(i)
        else:
            keep[i] = True  # unknown block kinds are kept — compaction is conservative
    for i in checkpoint_idx[-keep_checkpoints:]:
        keep[i] = True
    omitted = sum(1 for i in checkpoint_idx if not keep[i])
    out = [preamble] if preamble.strip() else []
    emitted_marker = False
    for i, (title, body) in enumerate(blocks):
        if keep[i]:
            out.append(title + ("\n" + body if body.strip() else ""))
        elif not emitted_marker:
            out.append(f"[compacted: {omitted} earlier checkpoint/validator "
                       f"entries omitted — full log on disk]")
            emitted_marker = True
    return "\n\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Kernel emission — the outbound half of the goal directory
#
# 21-kernel-wiring-gap.md section 7: "Goalkeeper emits a kernel transition event
# on every state change, and stores nothing that another component is expected to
# read." On 2026-08-30 a goal parked at 15:25:02Z and waited 1h46m while a
# watchdog reported "0 stalled, 0 blocked" every 20 minutes, because the only
# record of the park was `.claude/goals/*.json` — files other components
# reverse-engineered and got wrong when v0.8.0 changed their shape. The
# correction is not a better file layout. It is that gk announces, and consumers
# read the announcement instead of the files.
#
# What crosses to the kernel, and what deliberately does not:
#
#   * An audit EVENT on every state change, subject = the goal. gk does NOT own
#     or update an Initiative projection. Its state machine is domain-internal:
#     mirroring it into a kernel record would create two sources of truth that
#     drift, and `put_record` commits its own transaction, so a transition event
#     and a projection update cannot be made atomic from out here anyway.
#   * A raised CONDITION when a goal reaches a state that needs a person. That
#     is the thing a human must actually see, and the thing that failed on
#     2026-08-30. It is resolved when the goal is unblocked.
#
# BEST-EFFORT, AND DELIBERATELY THE OPPOSITE OF THE ADAPTER'S DEFAULT.
# runtime/kernel_adapter.py has no best-effort mode on purpose: a runtime
# component that cannot record a signal must stop. gk is the opposite case. It
# runs in fresh clones, worktrees, other people's checkouts, and machines where
# the kernel does not exist at all. A missing or broken kernel must never block,
# fail, or refuse a gk state change — gk is a state machine that has to keep
# working when it cannot phone home, and failing closed on infrastructure it does
# not own would be worse than the problem being fixed. So every emission failure
# is swallowed and surfaced as one line on stderr: never an exception, never a
# non-zero exit, never a refused transition.
#
# The adapter is therefore discovered at CALL time and never imported at module
# scope: `OSD_KERNEL_ADAPTER`, else a small list of documented default paths.
# Not found → silent no-op. Found but broken → one stderr line, and gk continues.
# ─────────────────────────────────────────────────────────────────────────────

KERNEL_ADAPTER_ENV = "OSD_KERNEL_ADAPTER"
KERNEL_EMIT_ENV = "GK_KERNEL_EMIT"
KERNEL_DECISION_OWNER_ENV = "GK_DECISION_OWNER"
KERNEL_DOMAIN_ENV = "GK_KERNEL_DOMAIN"
# Deliberately NOT GK_ACTOR: that one names the executing agent for the
# activation-time `executor` observables, and a harness setting it must not
# silently rewrite who the kernel records as the writer.
KERNEL_ACTOR_ENV = "GK_KERNEL_ACTOR"

KERNEL_ADAPTER_DEFAULTS = (
    "~/Documents/operation-system-design/runtime/kernel_adapter.py",
    "~/operation-system-design/runtime/kernel_adapter.py",
)
KERNEL_ACTOR = "process:goalkeeper"
KERNEL_DOMAIN = "kernel:shared-control"
KERNEL_DECISION_OWNER = "actor:chef"

# One live Signal per goal, per 02 section 5.15: the key excludes signal type and
# severity so both may change without splitting the condition in two.
NEEDS_HUMAN_CONDITION = "goalkeeper-awaiting-human"

# A checkpoint message is audit content, not a pointer at one — the log stops
# being an integration surface, so the event has to carry the text. Capped
# because a checkpoint is occasionally a pasted validator dump.
EMIT_TEXT_CAP = 4000

_GK_VERSION: Optional[str] = None


def gk_version() -> str:
    """Plugin version, read from the manifest so there is one place to bump."""
    global _GK_VERSION
    if _GK_VERSION is None:
        meta = read_json(Path(__file__).resolve().parents[1]
                         / ".claude-plugin" / "plugin.json") or {}
        _GK_VERSION = str(meta.get("version") or "unknown")
    return _GK_VERSION


def _emission_enabled() -> bool:
    return os.environ.get(KERNEL_EMIT_ENV, "").strip().lower() not in (
        "0", "off", "false", "no")


def _kernel_adapter_path(goals: Path):
    """(path, explicit) — where the adapter is, and whether a human said so.

    An explicit `OSD_KERNEL_ADAPTER` that does not resolve is a misconfiguration
    and earns the stderr line; a plain absence is the normal case on most
    machines and stays silent.
    """
    override = os.environ.get(KERNEL_ADAPTER_ENV, "").strip()
    if override:
        cand = Path(override).expanduser()
        if cand.is_dir():
            cand = cand / "kernel_adapter.py"
        return (cand if cand.is_file() else None), True
    root = project_root(goals)
    candidates = [root.parent / "operation-system-design" / "runtime"
                  / "kernel_adapter.py"]
    candidates += [Path(p).expanduser() for p in KERNEL_ADAPTER_DEFAULTS]
    for cand in candidates:
        if cand.is_file():
            return cand, False
    return None, False


def _import_adapter(path: Path):
    import importlib.util
    name = "gk_kernel_adapter"
    existing = sys.modules.get(name)
    origin = getattr(existing, "__file__", None) if existing else None
    if origin and Path(origin).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"no loader for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _kernel_note(detail: str) -> None:
    print(f"gk: kernel emission skipped ({detail}) — the state change is "
          f"recorded on disk and gk continues", file=sys.stderr)


def _with_kernel(goals: Path, action) -> None:
    """Run `action(adapter, fold)` against the kernel, or don't. Never raises.

    KeyboardInterrupt is the one thing that still propagates: a caller pressing
    ctrl-c is not an emission failure.
    """
    if not _emission_enabled():
        return
    path, explicit = _kernel_adapter_path(goals)
    if path is None:
        if explicit:
            _kernel_note(f"{KERNEL_ADAPTER_ENV} does not name a readable "
                         f"kernel_adapter.py")
        return
    try:
        adapter = _import_adapter(path)
        with adapter.emitter(
            actor_ref=os.environ.get(KERNEL_ACTOR_ENV) or KERNEL_ACTOR,
            domain_id=os.environ.get(KERNEL_DOMAIN_ENV) or KERNEL_DOMAIN,
        ) as fold:
            action(adapter, fold)
    except KeyboardInterrupt:
        raise
    except (Exception, SystemExit) as exc:  # a rogue adapter may even sys.exit
        _kernel_note(f"{type(exc).__name__}: {exc}")


def goal_ref(goals: Path, slug: str) -> str:
    """Stable kernel subject for a goal. Repo-qualified: two checkouts may hold
    the same slug, and a Signal deduplicates on (subject_ref, condition_key)."""
    return f"gk:goal:{project_root(goals).name}:{slug}"


def _chain_summary(chain: Optional[dict]) -> Optional[dict]:
    if not chain:
        return None
    slugs = chain.get("slugs") or []
    return {
        "name": chain.get("name"),
        "status": chain.get("status"),
        "cursor": chain.get("cursor"),
        "total": len(slugs),
        "slugs": slugs,
        "link_approvals": chain.get("link_approvals") or [],
        "waiting_since": chain.get("waiting_since"),
    }


def _emit_payload(goals: Path, slug: str, state: Optional[dict] = None,
                  extra: Optional[dict] = None) -> dict:
    """Everything an outside reader used to reverse-engineer from the files.

    `goal_chain_watchdog.py` read active.json's slug plus state.json's status,
    rejection_count and last_judge_verdict, and derived idleness from file
    mtimes. `cross_flow_relay.py` read receipt.json. All of it is here, on every
    event, so neither has to open a goal directory again.
    """
    if state is None:
        state = load_state(goals, slug) or {}
    payload = {
        "slug": slug,
        "goal_ref": goal_ref(goals, slug),
        "repo_root": str(project_root(goals)),
        "gk_version": gk_version(),
        "status": state.get("status"),
        "rejection_count": state.get("rejection_count"),
        "started_at": state.get("started_at"),
        "started_at_commit": state.get("started_at_commit"),
        "last_checkpoint_at": state.get("last_checkpoint_at"),
        "last_validator_result": state.get("last_validator_result"),
        "last_judge_verdict": state.get("last_judge_verdict"),
        "last_judge_mode": state.get("last_judge_mode"),
        "chain_step": state.get("chain_step"),
        "needs": state.get("needs"),
        "chain": _chain_summary(read_json(goals / "chain.json")),
    }
    payload.update(extra or {})
    return payload


def _sources(adapter, goals: Path, slug: str):
    return [adapter.Source(
        source_type="application",
        locator=f"gk:{project_root(goals)}/.claude/goals/{slug}",
        authority_scope="goalkeeper goal lifecycle",
        version_or_hash=f"goalkeeper-{gk_version()}",
        retention_rule="retain until the goal closes",
    )]


def emit_goal_event(goals: Path, slug: str, event_type: str, reason: str,
                    extra: Optional[dict] = None,
                    state: Optional[dict] = None,
                    escalate: Optional[dict] = None,
                    stand_down: Optional[str] = None) -> None:
    """One audit event, plus the human-gated condition when there is one.

    `escalate` raises (or updates) the live Signal for this goal;
    `stand_down` resolves it. Both are optional and both go through the same
    best-effort wrapper as the event: a goal still parks, and still resumes,
    when the kernel is not there.
    """
    payload = _emit_payload(goals, slug, state=state, extra=extra)
    subject = payload["goal_ref"]

    def action(adapter, fold):
        fold.record_audit_event(event_type=event_type, subject_ref=subject,
                                reason=reason, payload=payload)
        if escalate:
            fold.raise_condition(
                signal_type=escalate["signal_type"],
                subject_ref=subject,
                condition_key=NEEDS_HUMAN_CONDITION,
                severity=escalate.get("severity", "attention"),
                reason=escalate["reason"],
                decision_owner_ref=os.environ.get(KERNEL_DECISION_OWNER_ENV)
                or KERNEL_DECISION_OWNER,
                evidence_refs=[subject],
                # gk knows of no deadline here and declines to invent one. The
                # condition is still live in the fold (`open_signals`); it simply
                # joins no due-by queue. 21 section 6.2 leaves that horizon an
                # owner decision.
                required_response_by=None,
                sources=_sources(adapter, goals, slug),
                freshness=adapter.Freshness.ttl("live"),
                claim_quality="observed",
            )
        if stand_down:
            fold.resolve_condition(
                subject_ref=subject,
                condition_key=NEEDS_HUMAN_CONDITION,
                reason=stand_down,
                sources=_sources(adapter, goals, slug),
            )

    _with_kernel(goals, action)


def emit_mission_event(goals: Path, mission: dict, event_type: str,
                       reason: str, extra: Optional[dict] = None,
                       escalate: Optional[dict] = None,
                       stand_down: Optional[str] = None) -> None:
    """The mission layer's transitions, on the same terms as a goal's.

    An escalated mission is a person-shaped stop exactly like a parked goal, and
    leaving it invisible would reproduce the 2026-08-30 failure one level up.
    """
    name = mission.get("name") or "unnamed-mission"
    subject = f"gk:mission:{project_root(goals).name}:{name}"
    payload = {
        "mission": name,
        "mission_ref": subject,
        "repo_root": str(project_root(goals)),
        "gk_version": gk_version(),
        "status": mission.get("status"),
        "started_at": mission.get("started_at"),
        "goals_completed": mission.get("goals_completed") or [],
        "supervisor_verdicts": mission.get("supervisor_verdicts") or [],
    }
    payload.update(extra or {})

    def action(adapter, fold):
        fold.record_audit_event(event_type=event_type, subject_ref=subject,
                                reason=reason, payload=payload)
        sources = [adapter.Source(
            source_type="application",
            locator=f"gk:{project_root(goals)}/.claude/mission.json",
            authority_scope="goalkeeper mission supervision",
            version_or_hash=f"goalkeeper-{gk_version()}",
            retention_rule="retain until the mission closes",
        )]
        if escalate:
            fold.raise_condition(
                signal_type=escalate["signal_type"],
                subject_ref=subject,
                condition_key=NEEDS_HUMAN_CONDITION,
                severity=escalate.get("severity", "attention"),
                reason=escalate["reason"],
                decision_owner_ref=os.environ.get(KERNEL_DECISION_OWNER_ENV)
                or KERNEL_DECISION_OWNER,
                evidence_refs=[subject],
                required_response_by=None,
                sources=sources,
                freshness=adapter.Freshness.ttl("live"),
                claim_quality="observed",
            )
        if stand_down:
            fold.resolve_condition(subject_ref=subject,
                                   condition_key=NEEDS_HUMAN_CONDITION,
                                   reason=stand_down, sources=sources)

    _with_kernel(goals, action)


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

def contract_meta(goals: Path, slug: str) -> dict:
    """Frontmatter if there is any. Unlike load_contract this never dies —
    status is a read-only diagnostic and must survive a half-built goal dir."""
    path = goals / slug / "contract.md"
    if not path.is_file():
        return {}
    meta, _ = parse_frontmatter(path.read_text())
    return meta


def last_log_line(goals: Path, slug: str) -> str:
    log_path = goals / slug / "log.md"
    if not log_path.is_file():
        return "—"
    _, blocks = split_log_blocks(log_path.read_text())
    if not blocks:
        return "—"
    title, body = blocks[-1]
    first = next((l for l in body.splitlines() if l.strip()), "")
    return f"{title[3:]} | {first[:100]}"


def status_model(goals: Path) -> dict:
    """One derivation of what is true right now. 21 section 5.1.

    `gk status --json` used to dump four raw files and return before the parked
    derivation the text path performs, so a parked goal reported `"state": null`
    to a machine while printing the queue correctly to a human — the machine
    surface was strictly less informative than the human one, and every feature
    added below the early return was invisible by default. There is now one model
    and two renderings of it; nothing may be computed in a renderer.
    """
    slug = active_info(goals)
    parked = [{
        "slug": s,
        "needs": st.get("needs"),
        "needs_human_at": st.get("needs_human_at"),
        "rejection_count": st.get("rejection_count", 0),
        "chain_step": st.get("chain_step"),
        "last_judge_verdict": st.get("last_judge_verdict"),
    } for s, st in _parked_goals(goals)]
    state = load_state(goals, slug) if slug else None
    goal = None
    if slug:
        st = state or {}
        meta = contract_meta(goals, slug)
        goal = {
            "slug": slug,
            "objective": meta.get("objective"),
            "status": st.get("status"),
            "rejection_count": st.get("rejection_count", 0),
            "max_rejections": meta.get("max_rejections") or 5,
            "started_at": st.get("started_at"),
            "last_checkpoint_at": st.get("last_checkpoint_at"),
            "last_validator_result": st.get("last_validator_result"),
            "last_judge_verdict": st.get("last_judge_verdict"),
            "last_judge_mode": st.get("last_judge_mode"),
            "chain_step": st.get("chain_step"),
            "last_log": last_log_line(goals, slug),
        }
    return {
        "generated_at": now_iso(),
        "gk_version": gk_version(),
        "slug": slug,
        "status": (state or {}).get("status") if slug else None,
        "needs_human": bool(parked),
        "parked": parked,
        "goal": goal,
        # The four raw files stay, verbatim and under their original keys, so a
        # reader written against the old --json output keeps working.
        "active": read_json(goals / "active.json"),
        "state": state,
        "chain": read_json(goals / "chain.json"),
        "mission": read_mission(goals),
    }


def cmd_status(args) -> int:
    goals = find_goals_dir()
    model = status_model(goals)
    if args.json:
        print(json.dumps(model, indent=2))
        return 0
    mission = model["mission"]
    if mission:
        print(f"Mission:     {mission.get('name')}  ({mission.get('status')}, "
              f"{len(mission.get('goals_completed', []))} goals completed)")
    goal = model["goal"]
    if not goal:
        term = model["active"] or {}
        prev = term.get("previous_slug")
        extra = f" Last: {prev} ({term.get('ended_reason')})." if prev else ""
        print(f"No active goal.{extra} Run /goal-prep \"<rough idea>\" or "
              f"/goal \"<objective>\" to start one.")
        _render_parked(model["parked"])
        return 0
    chain = model["chain"]
    print(f"Goal:        {goal['slug']}")
    print(f"Objective:   {goal['objective'] or '—'}")
    print(f"Status:      {goal['status'] or '?'}   "
          f"Rejections: {goal['rejection_count']}/{goal['max_rejections']}")
    print(f"Started:     {goal['started_at'] or '—'}")
    print(f"Validator:   {goal['last_validator_result']}")
    print(f"Judge:       {goal['last_judge_verdict']}")
    print(f"Last log:    {goal['last_log']}")
    if chain and chain.get("status") in ("active", "waiting"):
        waiting = "  WAITING on a parked goal" if \
            chain.get("status") == "waiting" else ""
        print(f"Chain:       {chain['name']}  "
              f"[{chain['cursor']}/{len(chain['slugs'])} approved]{waiting}")
    if goal["status"] == "needs_human":
        print("\nNEEDS_HUMAN — see the latest 'judge rejected' block in "
              f"{goals / goal['slug'] / 'log.md'} — fix the listed items, "
              f"then /goal-resume or /goal-clear.")
    _render_parked([p for p in model["parked"] if p["slug"] != goal["slug"]])
    return 0


def _caller_observables() -> dict:
    """Best-effort caller identity — observed evidence, not authenticated
    identity. GK_ACTOR lets a harness name the acting agent explicitly."""
    obs = {}
    try:
        import getpass
        obs["user"] = getpass.getuser()
    except Exception:
        pass
    actor = os.environ.get("GK_ACTOR") or os.environ.get("CLAUDE_SESSION_ID")
    if actor:
        obs["env_actor"] = actor
    return obs


def _activate(goals: Path, slug: str, chain_name: Optional[str] = None,
              chain_step: Optional[int] = None) -> None:
    """Shared activation mechanics — state.json + active.json + log entry."""
    meta, _, _ = load_contract(goals, slug)
    root = project_root(goals)
    commit, dirty = git_baseline(root)
    state = {
        "status": "active",
        "rejection_count": 0,
        "started_at": now_iso(),
        "started_at_commit": commit,
        "started_at_dirty_paths": dirty,
        "last_checkpoint_at": None,
        "last_validator_result": None,
        "last_judge_verdict": None,
        # Verdict provenance (v1): judge verdicts require a token minted by
        # `gk judge-brief`; the executed judge mode is persisted per verdict.
        # Goals activated before this field existed complete under their
        # activation-time rules (no token required).
        "provenance_version": 1,
        "executor": {**_caller_observables(), "recorded_at": now_iso()},
        "judge_verdicts": [],
    }
    if chain_step is not None:
        state["chain_step"] = chain_step
    baseline = read_json(goals / slug / "baseline.json")
    if baseline:
        state["validator_baseline_result"] = baseline.get("result")
        state["validator_baseline_failing_paths"] = baseline.get("failing_paths", [])
    save_state(goals, slug, state)
    active = {"slug": slug, "activated_at": now_iso()}
    if chain_name:
        active["chain"] = chain_name
    write_json(goals / "active.json", active)
    short = (commit or "no-git")[:9]
    title = "activated" if chain_step is None else \
        f"activated (chain step {chain_step})"
    body = (f"Starting work on: {meta.get('objective', slug)}\n"
            f"Baseline commit: {short}")
    if dirty:
        body += f"\nPre-existing dirty paths at activation: {', '.join(dirty[:20])}"
    append_log(goals, slug, title, body)
    emit_goal_event(
        goals, slug, "goal.activated",
        f"goal '{slug}' activated"
        + (f" as chain step {chain_step}" if chain_step is not None else ""),
        extra={
            "from_status": None,
            "to_status": "active",
            "objective": meta.get("objective"),
            "max_rejections": meta.get("max_rejections") or 5,
            "judge_mode": meta.get("judge_mode") or "subagent",
            "started_at_dirty_paths": dirty,
            "chain_name": chain_name,
        },
        state=state)


def cmd_activate(args) -> int:
    goals = find_goals_dir(create=True)
    if not SLUG_RE.match(args.slug):
        die(f"invalid slug '{args.slug}' (kebab-case, 2-64 chars)")
    current = active_info(goals)
    if current and current != args.slug and not args.force:
        st = load_state(goals, current) or {}
        if st.get("status") in ("active", "paused", "needs_human"):
            die(f"goal '{current}' is {st.get('status')}. "
                f"/goal-clear it first, or pass --force.")
    _activate(goals, args.slug, chain_name=args.chain, chain_step=args.chain_step)
    print(f"Activated goal '{args.slug}'.")
    return 0


def cmd_baseline(args) -> int:
    """Run the validator once pre-activation; record the pre-existing result."""
    goals = find_goals_dir()
    meta, _, _ = load_contract(goals, args.slug)
    result, output = run_validator(project_root(goals), meta)
    kind = "pass" if result == "pass" else \
           ("not_runnable" if result == "not_runnable" else "fail")
    failing = [p.strip() for p in (args.paths or "").split(",") if p.strip()]
    if kind == "fail" and not failing:
        # naive extraction: output tokens that resolve to real files
        root = project_root(goals)
        seen = set()
        for tok in re.findall(r"[\w./-]+\.[\w]+", output):
            if tok not in seen and (root / tok).is_file():
                seen.add(tok)
                failing.append(tok)
        failing = failing[:50]
    write_json(goals / args.slug / "baseline.json",
               {"result": kind, "failing_paths": failing, "captured_at": now_iso()})
    print(f"Baseline: {kind}" + (f" ({len(failing)} failing paths)" if failing else ""))
    if kind == "not_runnable":
        print(f"  {result if result != 'not_runnable' else output}".rstrip())
    return 0


def _read_message(args) -> str:
    if getattr(args, "message", None):
        return args.message
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def cmd_checkpoint(args) -> int:
    goals = find_goals_dir()
    msg = _read_message(args)
    if not msg.strip():
        die("checkpoint needs a message (--message or stdin)")
    append_log(goals, args.slug, "checkpoint", msg)
    state = load_state(goals, args.slug)
    if state:
        state["last_checkpoint_at"] = now_iso()
        save_state(goals, args.slug, state)
    emit_goal_event(goals, args.slug, "goal.checkpointed",
                    f"checkpoint recorded for '{args.slug}'",
                    extra={"message": msg.strip()[:EMIT_TEXT_CAP]},
                    state=state)
    print("Checkpoint logged.")
    return 0


def cmd_validate(args) -> int:
    goals = find_goals_dir()
    meta, _, _ = load_contract(goals, args.slug)
    result, output = run_validator(project_root(goals), meta)
    state = load_state(goals, args.slug)
    if state:
        state["last_validator_result"] = result
        state["last_checkpoint_at"] = now_iso()
        save_state(goals, args.slug, state)
    passed = result == "pass"
    append_log(goals, args.slug, f"validator {'passed' if passed else 'failed'}",
               "" if passed else result)
    emit_goal_event(goals, args.slug, "goal.validated",
                    f"validator {'passed' if passed else 'failed'} for "
                    f"'{args.slug}'",
                    extra={"result": result, "passed": passed,
                           "command": (meta.get("validator") or {}).get("command"),
                           "output_tail": tail(output)[:EMIT_TEXT_CAP]},
                    state=state)
    print(f"VALIDATOR: {result}")
    t = tail(output)
    if t:
        print("--- output tail ---")
        print(t)
    return 0 if passed else 1


JUDGE_TASK = """\
# Your task

**Output the verdict ONCE.** Pre-think your reasoning before producing the
structured response. Do not self-correct or revise individual DoD lines
mid-response — finalize each MET/NOT MET decision before writing the verdict.

For each item in `definition_of_done`, decide whether it is met. Use BOTH the
diff above AND the Read tool to read each modified/added file in full — diffs
hide context. Be strict:

- A criterion is "met" only if the diff or files demonstrate it concretely.
  "Probably done" = not met.
- **Every MET verdict MUST cite at least one file:line (or named file section)
  as evidence. A MET line without a concrete citation is invalid — re-examine
  the criterion before writing it.**
- Watch for placeholders, stubs, .todo markers, skipped tests, commented-out
  work, or "TODO: real implementation" comments. AUTOMATIC rejection
  regardless of validator status.
- Watch for tests that assert existence rather than behavior
  (`expect(fn).toBeDefined()` is not a test).
- Watch for non-goal violations — the contract's `non_goals` list is binding.
- Watch for changes to pre-existing dirty paths that may not be the goal's intent.
- The validator passing is necessary but NOT sufficient. Do not approve solely
  because the validator exited zero.

Respond in this exact format:

VERDICT: approve
or
VERDICT: reject

REASONS:
- <one bullet per DoD item: "MET <file:line evidence>" or "NOT MET" with a
  one-sentence justification>
- Non-goal violations: NONE / <list>
- Anti-placeholder check: CLEAN / <findings>
- Pre-existing-dirt check: NONE / <list of suspicious paths>
- Pre-existing validator-failure check: NONE / <paths failing at baseline that
  still fail; mark "not goal-caused">

FIX_LIST: (only if reject)
- <specific actionable item, one per problem, ordered by priority>

NOTES: (optional)
<non-blocking observations>
"""


def cmd_judge_brief(args) -> int:
    goals = find_goals_dir()
    slug = args.slug
    meta, _, cpath = load_contract(goals, slug)
    state = load_state(goals, slug) or {}
    root = project_root(goals)
    log_path = goals / slug / "log.md"
    log_text = log_path.read_text() if log_path.is_file() else "(no log)"

    excludes = list(DEFAULT_EXCLUDES)
    for glob in meta.get("diff_excludes") or []:
        excludes.append(f":!{glob}")
    includes = meta.get("diff_includes") or []
    pathspec = includes if includes else ["."] + excludes

    baseline = state.get("started_at_commit")
    diff_parts, files = [], []
    if baseline:
        committed = _git(root, "diff", f"{baseline}..HEAD", "--", *pathspec) or ""
        working = _git(root, "diff", "--", *pathspec) or ""
        untracked = (_git(root, "ls-files", "--others", "--exclude-standard",
                          "--", *pathspec) or "").strip()
        diff_parts = [p for p in (committed, working) if p.strip()]
        for out in (
            _git(root, "diff", "--name-only", f"{baseline}..HEAD", "--", *pathspec),
            _git(root, "diff", "--name-only", "--", *pathspec),
            untracked,
        ):
            for line in (out or "").splitlines():
                p = line.strip()
                if p and p not in files:
                    files.append(p)
        untracked_list = untracked.splitlines() if untracked else []
    else:
        untracked_list = []

    diff_text = "\n".join(diff_parts) if diff_parts else \
        ("(no committed or working-tree diff)" if baseline
         else "No git repo — review log + files only.")
    if len(diff_text) > DIFF_CAP_CHARS:
        diff_text = (diff_text[:DIFF_CAP_CHARS] +
                     f"\n\n[diff truncated at {DIFF_CAP_CHARS} chars — Read the "
                     f"files in the list above in full; they are authoritative]")

    dirty = state.get("started_at_dirty_paths") or []
    vb = state.get("validator_baseline_result")
    vb_paths = state.get("validator_baseline_failing_paths") or []

    executor_summary = ""
    if args.executor_summary:
        p = Path(args.executor_summary)
        if p.is_file():
            executor_summary = p.read_text()

    out = []
    out.append("You are an independent judge reviewing a goalkeeper goal. "
               "You have not seen the executing agent's reasoning — review the "
               "artifacts only.\n")
    out.append("# Contract\n")
    out.append(cpath.read_text())
    out.append("\n# Progress log (compacted — full log on disk at "
               f"{log_path})\n")
    out.append(compact_log(log_text))
    if executor_summary.strip():
        out.append("\n# Executor self-report (leading hint only — verify "
                   "independently; the executor's self-report is NOT "
                   "authoritative)\n")
        out.append(executor_summary)
    out.append("\n# Diff scope\n")
    out.append(f"Baseline: {(baseline or 'no-git')[:9]}")
    out.append(f"Validator baseline: {vb or 'unknown'}")
    out.append("Pre-existing validator-failing paths (failures on these are "
               "NOT goal-caused):")
    out.append("  " + (", ".join(vb_paths) if vb_paths else "none/unknown"))
    out.append("Default + contract exclusions applied (lockfiles, build "
               "outputs, coverage, IDE files).")
    out.append("Pre-existing dirty paths at activation (do NOT credit as goal "
               "work, but flag if goal work touched them):")
    out.append("  " + (", ".join(dirty) if dirty else "none"))
    out.append("\n# Files modified or added since baseline "
               "(Read each END-TO-END)\n")
    out.append("\n".join(str(root / f) for f in files) if files else "(none detected)")
    if untracked_list:
        out.append("\nUntracked new files (not in the diff below — Read them "
                   "in full):")
        out.append("\n".join(str(root / f) for f in untracked_list))
    out.append("\n# Diff (excerpt)\n")
    out.append(diff_text)
    out.append("")
    out.append(JUDGE_TASK)

    # Mint the single-use judge token. The verdict command consumes it —
    # provenance (which mode actually ran) is stamped here by the CLI, never
    # typed by the caller at verdict time. Mistranscription under drift is
    # the failure this closes; a hostile caller is out of scope by design.
    import secrets
    contract_mode = meta.get("judge_mode") or "subagent"
    mode = getattr(args, "mode", None) or contract_mode
    write_json(goals / slug / "judge-token.json", {
        "token_id": secrets.token_hex(8),
        "minted_at": now_iso(),
        "mode": mode,
        "contract_mode": contract_mode,
        "minted_by": _caller_observables(),
        "used": False,
    })
    print(f"[gk] judge token minted for '{slug}' (mode={mode}, single-use) — "
          f"the next `gk verdict {slug}` consumes it.", file=sys.stderr)

    print("\n".join(out))
    return 0


def _chain_active_at_cursor(goals: Path, slug: str) -> Optional[dict]:
    chain = read_json(goals / "chain.json")
    if not chain or chain.get("status") != "active":
        return None
    cursor = chain.get("cursor", 0)
    slugs = chain.get("slugs", [])
    if cursor < len(slugs) and slugs[cursor] == slug:
        return chain
    return None


def _extract_section(text: str, header: str) -> str:
    """Pull 'REASONS:' / 'FIX_LIST:' style sections from a judge response."""
    m = re.search(rf"^{header}:\s*$(.*?)(?=^\w[A-Z_]*:\s*$|\Z)",
                  text, re.MULTILINE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(rf"^{header}:(.*?)(?=^[A-Z][A-Z_]*:|\Z)",
                  text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _complete_chain(goals: Path, chain: dict, final_slug: str) -> None:
    chain["status"] = "done"
    chain["completed_at"] = now_iso()
    write_json(goals / "chain.json", chain)
    write_json(goals / "active.json", {
        "slug": None, "ended_at": now_iso(), "ended_reason": "chain_completed",
        "previous_slug": final_slug, "previous_chain": chain["name"],
    })
    emit_goal_event(goals, final_slug, "chain.completed",
                    f"chain '{chain['name']}' completed at '{final_slug}'",
                    extra={"ended_reason": "chain_completed",
                           "final_slug": final_slug})


def _advance_chain(goals: Path, chain: dict, approved_slug: str) -> str:
    """Mark done, record approval, bump cursor, activate next or complete.

    Returns 'CHAIN_COMPLETE' or 'NEXT: <slug>'.
    """
    state = load_state(goals, approved_slug) or {}
    prior_status = state.get("status")
    state["status"] = "done"
    save_state(goals, approved_slug, state)
    approvals = chain.setdefault("link_approvals", [])
    if not any(a.get("slug") == approved_slug for a in approvals):
        approvals.append({"slug": approved_slug, "approved_at": now_iso()})
    chain["cursor"] = chain.get("cursor", 0) + 1
    write_json(goals / "chain.json", chain)
    emit_goal_event(goals, approved_slug, "chain.advanced",
                    f"chain '{chain['name']}' advanced past '{approved_slug}'",
                    extra={"from_status": prior_status, "to_status": "done",
                           "approved_slug": approved_slug},
                    state=state)
    slugs = chain["slugs"]
    if chain["cursor"] >= len(slugs):
        _complete_chain(goals, chain, approved_slug)
        return "CHAIN_COMPLETE"
    next_slug = slugs[chain["cursor"]]
    _activate(goals, next_slug, chain_name=chain["name"],
              chain_step=chain["cursor"] + 1)
    return f"NEXT: {next_slug}"


def _write_receipt(goals: Path, slug: str, meta: dict, state: dict,
                   tok: dict, entry: dict) -> None:
    """Mint the self-contained verdict receipt (provenance-v1 goals only).

    The receipt is the exportable artifact a consumer outside this goals dir
    (a TaskFlow edge, a release gate, another repo's lane) can carry and
    re-verify: what was decided, by which judge mode, over which contract
    hash and repo commit, under which single-use token. Like the token, it
    is written by the CLI at the moment of the verdict — never typed by a
    caller — and hook-guarded against direct edits.
    """
    import hashlib
    root = project_root(goals)
    head, dirty = git_baseline(root)
    _, _, contract_path = load_contract(goals, slug)
    write_json(goals / slug / "receipt.json", {
        "receipt_version": 1,
        "slug": slug,
        "decision": entry["verdict"],
        "at": entry["at"],
        "mode": entry.get("mode"),
        "contract_mode": tok.get("contract_mode")
                         or (meta.get("judge_mode") or "subagent"),
        # gate_quality is the one bit a cross-flow consumer keys on: an
        # approve delivered by the subagent judge. Advisory inline verdicts
        # and rejections are never gate-quality.
        "gate_quality": entry["verdict"] == "approve"
                        and entry.get("mode") == "subagent",
        "token": {k: tok.get(k)
                  for k in ("token_id", "minted_at", "minted_by", "used_at")},
        "executor": state.get("executor"),
        "contract_sha256":
            hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "repo": {
            "head": head,
            "dirty_paths": dirty,
            "started_at_commit": state.get("started_at_commit"),
        },
        "rejection_count": state.get("rejection_count", 0),
        "verdict_index": len(state.get("judge_verdicts", [])) - 1,
    })


def cmd_verdict(args) -> int:
    goals = find_goals_dir()
    slug = args.slug
    state = load_state(goals, slug)
    if state is None:
        die(f"no state.json for '{slug}'")
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    reasons = _extract_section(raw, "REASONS") or raw.strip() or "(none provided)"
    meta, _, _ = load_contract(goals, slug)
    max_rej = meta.get("max_rejections") or 5

    # Verdict provenance: goals activated at provenance_version >= 1 accept a
    # verdict only against an unused token minted by `gk judge-brief`. The
    # executed mode is read from the token — caller-typed provenance is not
    # accepted. Pre-provenance goals complete under their activation-time
    # rules (F10: no in-flight goal is stranded by the cutover).
    prov = state.get("provenance_version") or 0
    tok = None
    mode = None
    if prov >= 1:
        tok = read_json(goals / slug / "judge-token.json")
        if not tok or tok.get("used"):
            die(f"no unused judge token for '{slug}' — run `gk judge-brief "
                f"{slug}` first; it mints the single-use token this verdict "
                f"consumes. Caller-typed provenance is not accepted.")
        mode = tok.get("mode") or "subagent"
        contract_mode = tok.get("contract_mode") or \
            (meta.get("judge_mode") or "subagent")
        if args.decision == "approve" and mode == "inline" \
                and contract_mode == "subagent":
            die(f"contract for '{slug}' requires judge_mode 'subagent' but "
                f"this token was minted inline (advisory only) — an inline "
                f"verdict does not convert into a gate-quality approval. "
                f"Re-run `gk judge-brief {slug}` and spawn the subagent judge.")
        tok["used"] = True
        tok["used_at"] = now_iso()
        write_json(goals / slug / "judge-token.json", tok)

    entry = {"at": now_iso(), "verdict": args.decision, "mode": mode}
    if tok:
        entry["token_id"] = tok.get("token_id")
        entry["token_minted_at"] = tok.get("minted_at")
    else:
        entry["legacy"] = True
    state.setdefault("judge_verdicts", []).append(entry)
    if mode:
        state["last_judge_mode"] = mode

    if args.decision == "approve":
        state["last_judge_verdict"] = "approve"
        state["approved_at"] = now_iso()
        save_state(goals, slug, state)
        append_log(goals, slug, "judge approved", f"Reasons:\n{reasons}")
        if tok:
            _write_receipt(goals, slug, meta, state, tok, entry)
        _emit_verdict(goals, slug, state, entry, reasons, "")
        chain = _chain_active_at_cursor(goals, slug)
        if chain:
            result = _advance_chain(goals, chain, slug)
            print(f"APPROVED: {slug}")
            print(result)
        else:
            state["status"] = "done"
            save_state(goals, slug, state)
            append_log(goals, slug, "done", "Judge approved; goal complete.")
            write_json(goals / "active.json", {
                "slug": None, "ended_at": now_iso(), "ended_reason": "done",
                "previous_slug": slug,
            })
            emit_goal_event(goals, slug, "goal.completed",
                            f"goal '{slug}' completed on judge approval",
                            extra={"from_status": "active", "to_status": "done",
                                   "ended_reason": "done",
                                   "approved_at": state.get("approved_at")},
                            state=state)
            print(f"APPROVED: {slug}")
            print("DONE")
        return 0

    # reject
    fix_list = _extract_section(raw, "FIX_LIST") or "(judge provided no fix-list)"
    state["last_judge_verdict"] = "reject"
    state["rejection_count"] = state.get("rejection_count", 0) + 1
    n = state["rejection_count"]
    if tok:
        _write_receipt(goals, slug, meta, state, tok, entry)
    append_log(goals, slug, "judge rejected",
               f"Reasons:\n{reasons}\n\nFix-list:\n{fix_list}\n\n"
               f"Rejection count: {n}/{max_rej}")
    if n >= max_rej:
        _emit_verdict(goals, slug, state, entry, reasons, fix_list,
                      max_rejections=max_rej)
        _park(goals, slug, state,
              f"review the judge's fix-list ({n}/{max_rej} rejections), then "
              f"`gk resume {slug} --reset-rejections` or `gk clear`",
              "parked (max rejections)",
              # 06 section 10.1 precedence: `validation_failed` is more specific
              # than `hard_dependency_broken` when the gate itself is what
              # refused, and both outrank `state_stale`.
              signal_type="validation_failed",
              signal_reason=f"goal '{slug}' exhausted its rejection budget "
                            f"({n}/{max_rej}) and is waiting for a person")
        print(f"REJECTED: {slug}  ({n}/{max_rej})")
        print("NEEDS_HUMAN")
    else:
        save_state(goals, slug, state)
        _emit_verdict(goals, slug, state, entry, reasons, fix_list,
                      max_rejections=max_rej)
        print(f"REJECTED: {slug}  ({n}/{max_rej})")
        print("RETRY")
    return 0


def _emit_verdict(goals: Path, slug: str, state: dict, entry: dict,
                  reasons: str, fix_list: str,
                  max_rejections: Optional[int] = None) -> None:
    """The judged event, carrying the receipt inline.

    `cross_flow_relay.py` reads `receipt.json` out of the goal directory today;
    the whole receipt travels here so it does not have to. It is the same object
    `gk receipt` prints — minted by the CLI at the moment the token was
    consumed, never typed by a caller.
    """
    outcome = "approved" if entry["verdict"] == "approve" else "rejected"
    emit_goal_event(
        goals, slug, "goal.judged", f"judge {outcome} '{slug}'",
        extra={
            "verdict": entry["verdict"],
            "judge_mode": entry.get("mode"),
            "token_id": entry.get("token_id"),
            "legacy_verdict": bool(entry.get("legacy")),
            "max_rejections": max_rejections,
            "reasons": reasons[:EMIT_TEXT_CAP],
            "fix_list": fix_list[:EMIT_TEXT_CAP] or None,
            "receipt": read_json(goals / slug / "receipt.json"),
        },
        state=state)


def cmd_receipt(args) -> int:
    """Print the goal's verdict receipt — the exportable provenance artifact."""
    goals = find_goals_dir()
    receipt = read_json(goals / args.slug / "receipt.json")
    if receipt is None:
        state = load_state(goals, args.slug) or {}
        if not (state.get("provenance_version") or 0):
            die(f"'{args.slug}' is a pre-provenance goal — receipts exist "
                f"only for goals activated at provenance_version >= 1.")
        die(f"no receipt for '{args.slug}' — a receipt is minted when "
            f"`gk verdict` consumes a judge token. No verdict has been "
            f"accepted yet.")
    print(json.dumps(receipt, indent=2))
    return 0


def cmd_chain_start(args) -> int:
    goals = find_goals_dir(create=True)
    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        die(f"chain file not found: {src}")
    meta, body = parse_frontmatter(src.read_text())
    name = meta.get("name") or src.stem
    slugs = []
    for line in body.splitlines():
        m = re.match(r"^\s*(?:\d+\.|[-*])\s+(\S+)", line)
        if m:
            slug = m.group(1).split("#")[0].strip()
            if slug and SLUG_RE.match(slug):
                slugs.append(slug)
    if not slugs:
        die("no slugs found in chain file (numbered or bulleted list expected)")
    missing = [s for s in slugs if not (goals / s / "contract.md").is_file()]
    if missing:
        die("missing contracts for: " + ", ".join(missing) +
            ". Run /goal-prep for each, or remove them from the chain file.")
    current = active_info(goals)
    if current:
        st = load_state(goals, current) or {}
        if st.get("status") in ("active", "paused", "needs_human"):
            die(f"goal '{current}' is {st.get('status')} — /goal-clear first.")
    existing_chain = read_json(goals / "chain.json")
    if existing_chain and existing_chain.get("status") in ("active", "waiting"):
        die(f"chain '{existing_chain.get('name')}' is "
            f"{existing_chain.get('status')} — resume its parked goal or "
            f"/goal-clear first. (Parallel lanes belong in worktrees.)")
    write_json(goals / "chain.json", {
        "name": name, "slugs": slugs, "cursor": 0, "status": "active",
        "started_at": now_iso(), "completed_at": None,
        "source_file": str(src), "link_approvals": [],
    })
    emit_goal_event(goals, slugs[0], "chain.started",
                    f"chain '{name}' started with {len(slugs)} goals",
                    extra={"chain_name": name, "source_file": str(src)})
    _activate(goals, slugs[0], chain_name=name, chain_step=1)
    print(f"Chain '{name}' started: {len(slugs)} goals.")
    print(f"NEXT: {slugs[0]}")
    return 0


def cmd_advance(args) -> int:
    """Manual/recovery advance — normal flow goes through `gk verdict approve`."""
    goals = find_goals_dir()
    chain = read_json(goals / "chain.json")
    if not chain or chain.get("status") != "active":
        die("no active chain")
    cursor = chain.get("cursor", 0)
    slugs = chain.get("slugs", [])
    if cursor >= len(slugs):
        _complete_chain(goals, chain, slugs[-1] if slugs else "?")
        print("CHAIN_COMPLETE")
        return 0
    slug = slugs[cursor]
    state = load_state(goals, slug) or {}
    if state.get("last_judge_verdict") != "approve" and not args.fix:
        die(f"'{slug}' at cursor has no judge approval — advance refuses. "
            f"Pass --fix to force (recovery only).")
    print(_advance_chain(goals, chain, slug))
    return 0


def cmd_pause(args) -> int:
    goals = find_goals_dir()
    slug = active_info(goals)
    if not slug:
        die("no active goal")
    state = load_state(goals, slug) or {}
    if state.get("status") != "active":
        print(f"Goal '{slug}' is {state.get('status')} — nothing to pause.")
        return 0
    state["status"] = "paused"
    state["paused_at"] = now_iso()
    save_state(goals, slug, state)
    append_log(goals, slug, "paused",
               "Paused by user. No further iterations until /goal-resume.")
    emit_goal_event(goals, slug, "goal.paused",
                    f"goal '{slug}' paused by user",
                    extra={"from_status": "active", "to_status": "paused"},
                    state=state)
    print(f"Paused goal '{slug}'. Resume with /goal-resume.")
    return 0


def _park(goals: Path, slug: str, state: dict, needs: str, title: str,
          signal_type: str = "hard_dependency_broken",
          signal_reason: Optional[str] = None) -> None:
    """needs_human parks the GOAL, never the agent.

    Record exactly what a person must provide, free the active slot so other
    goals can run, and put the goal's chain (if any) into 'waiting'. The goal
    sits in the parked queue (`gk status`) until a human unblocks it and work
    resumes with `gk resume <slug>`.

    This is the moment 21 section 3 is about. Alongside the local writes it
    raises the kernel condition that says a person is now the blocker — the
    thing nothing outside gk could learn on 2026-08-30. Severity is `attention`
    for both park paths: 06 section 9 sets severity from the required response
    horizon, and gk knows of no deadline here, so it declines to manufacture one.
    """
    prior_status = state.get("status")
    state["status"] = "needs_human"
    state["needs_human_at"] = now_iso()
    state["needs"] = needs
    save_state(goals, slug, state)
    freed_slot = active_info(goals) == slug
    if freed_slot:
        write_json(goals / "active.json", {
            "slug": None, "ended_at": now_iso(), "ended_reason": "parked",
            "previous_slug": slug,
        })
    chain = read_json(goals / "chain.json")
    chain_waiting = False
    if chain and chain.get("status") == "active":
        cursor = chain.get("cursor", 0)
        slugs = chain.get("slugs", [])
        if cursor < len(slugs) and slugs[cursor] == slug:
            chain["status"] = "waiting"
            chain["waiting_since"] = now_iso()
            write_json(goals / "chain.json", chain)
            chain_waiting = True
    append_log(goals, slug, title,
               f"Needs a human: {needs}\n"
               f"The active slot is free — continue with other work. "
               f"Unblock, then `gk resume {slug}`.")
    emit_goal_event(
        goals, slug, "goal.parked", f"goal '{slug}' parked: {needs}",
        extra={"from_status": prior_status, "to_status": "needs_human",
               "needs": needs, "park_reason": title,
               "freed_active_slot": freed_slot,
               "chain_moved_to_waiting": chain_waiting},
        state=state,
        escalate={
            "signal_type": signal_type,
            "severity": "attention",
            "reason": signal_reason
            or f"goal '{slug}' is parked and cannot proceed without a person: "
               f"{needs}",
        })


def _parked_goals(goals: Path) -> list:
    """(slug, state) for every goal sitting in needs_human."""
    out = []
    for d in sorted(goals.iterdir()):
        if not d.is_dir() or d.name in ("_archive", "shared"):
            continue
        st = read_json(d / "state.json")
        if st and st.get("status") == "needs_human":
            out.append((d.name, st))
    return out


def _render_parked(parked: list) -> None:
    """Renderer only — the queue itself is derived once, in status_model()."""
    if not parked:
        return
    print("\nParked (needs a human — everything else may proceed):")
    for p in parked:
        print(f"  {p['slug']} — since {p.get('needs_human_at') or '?'}")
        print(f"    needs: {p.get('needs') or 'see the goal log'}")
        print(f"    unblock, then: gk resume {p['slug']}")


def cmd_park(args) -> int:
    """Park a goal on a human-gated blocker and free the active slot."""
    goals = find_goals_dir()
    slug = args.slug or active_info(goals)
    if not slug:
        die("no active goal and no slug given")
    state = load_state(goals, slug)
    if state is None:
        die(f"unknown goal '{slug}'")
    if state.get("status") not in ("active", "paused"):
        die(f"goal '{slug}' is {state.get('status')} — only an active or "
            f"paused goal can be parked")
    _park(goals, slug, state, args.needs, "parked")
    print(f"Parked goal '{slug}'.")
    print(f"NEEDS: {args.needs}")
    print("The active slot is free — continue with other work.")
    return 0


def cmd_resume(args) -> int:
    goals = find_goals_dir()
    active = active_info(goals)
    slug = args.slug or active
    if not slug:
        parked = _parked_goals(goals)
        if parked:
            die("no active goal — name the goal to resume: gk resume <slug>. "
                "Parked: " + ", ".join(s for s, _ in parked))
        die("no active goal")
    if active and active != slug:
        die(f"goal '{active}' holds the active slot — one loop at a time. "
            f"Finish, park, or clear it before resuming '{slug}'.")
    state = load_state(goals, slug)
    if state is None:
        die(f"unknown goal '{slug}'")
    status = state.get("status")
    if status == "active":
        print(f"Goal '{slug}' is already active.")
        return 0
    if status == "done":
        die(f"goal '{slug}' is done — goals don't reopen. /goal-clear to archive.")
    if status == "needs_human" and state.get("rejection_count", 0) > 0 and \
            not (args.reset_rejections or args.keep_count):
        die("resuming from needs_human with rejections on the clock requires "
            "an explicit choice: --reset-rejections (issues fixed) or "
            "--keep-count. The skill must ask the user first.")
    state["status"] = "active"
    state["resumed_at"] = now_iso()
    state.pop("needs", None)
    note = "Resumed by user."
    if args.reset_rejections:
        state["rejection_count"] = 0
        note += " Rejection counter reset."
    save_state(goals, slug, state)
    chain_rearmed = False
    if active != slug:
        active_data = {"slug": slug, "activated_at": now_iso()}
        chain = read_json(goals / "chain.json")
        if chain and chain.get("status") == "waiting":
            cursor = chain.get("cursor", 0)
            slugs = chain.get("slugs", [])
            if cursor < len(slugs) and slugs[cursor] == slug:
                chain["status"] = "active"
                chain.pop("waiting_since", None)
                write_json(goals / "chain.json", chain)
                active_data["chain"] = chain["name"]
                chain_rearmed = True
        write_json(goals / "active.json", active_data)
    append_log(goals, slug, "resumed", note)
    emit_goal_event(
        goals, slug, "goal.resumed", f"goal '{slug}' resumed by user",
        extra={"from_status": status, "to_status": "active",
               "reset_rejections": bool(args.reset_rejections),
               "chain_rearmed": chain_rearmed},
        state=state,
        # The person showed up. A live condition that is not stood down is worse
        # than none: the next reader cannot tell a real blocker from a stale one.
        stand_down=(f"goal '{slug}' resumed — the human-gated blocker is cleared"
                    if status == "needs_human" else None))
    print(f"Resumed goal '{slug}'.")
    return 0


def cmd_clear(args) -> int:
    goals = find_goals_dir()
    slug = active_info(goals)
    if not slug:
        die("no active goal")
    if not args.yes:
        die("clear requires --yes (the skill confirms with the user first)")
    state = load_state(goals, slug) or {}
    append_log(goals, slug, "cleared",
               f"Final status before clear: {state.get('status')}. "
               f"Rejection count: {state.get('rejection_count', 0)}. Archived.")
    chain = read_json(goals / "chain.json")
    terminal = {
        "slug": None, "ended_at": now_iso(), "ended_reason": "cleared",
        "previous_slug": slug,
    }
    if chain and chain.get("status") == "active" and slug in chain.get("slugs", []):
        chain["status"] = "aborted"
        chain["completed_at"] = now_iso()
        write_json(goals / "chain.json", chain)
        append_log(goals, slug, "chain aborted",
                   f"Chain '{chain['name']}' aborted by /goal-clear.")
        terminal["previous_chain"] = chain["name"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = goals / "_archive"
    archive.mkdir(exist_ok=True)
    dest = archive / f"{slug}-{stamp}"
    # Emit before the move: afterwards the goal dir is gone and the payload
    # builder would report an empty state for a goal that had one.
    if chain and chain.get("status") == "aborted":
        emit_goal_event(goals, slug, "chain.aborted",
                        f"chain '{chain['name']}' aborted by clear of '{slug}'",
                        extra={"chain_name": chain["name"]}, state=state)
    emit_goal_event(
        goals, slug, "goal.cleared", f"goal '{slug}' cleared and archived",
        extra={"from_status": state.get("status"), "to_status": "cleared",
               "ended_reason": "cleared", "archived_to": str(dest)},
        state=state,
        stand_down=(f"goal '{slug}' cleared — the human-gated blocker no longer "
                    f"applies" if state.get("status") == "needs_human" else None))
    shutil.move(str(goals / slug), str(dest))
    write_json(goals / "active.json", terminal)
    print(f"Cleared goal '{slug}'. Archived to {dest}.")
    return 0


def cmd_log(args) -> int:
    goals = find_goals_dir()
    log_path = goals / args.slug / "log.md"
    if not log_path.is_file():
        # check archive
        archive = goals / "_archive"
        cands = sorted(archive.glob(f"{args.slug}-*")) if archive.is_dir() else []
        if cands:
            log_path = cands[-1] / "log.md"
    if not log_path.is_file():
        die(f"no log for '{args.slug}'")
    text = log_path.read_text()
    print(compact_log(text, args.checkpoints) if args.compact else text)
    return 0


def cmd_doctor(args) -> int:
    """Detect (and with --fix repair) inconsistent chain state."""
    goals = find_goals_dir()
    chain = read_json(goals / "chain.json")
    problems = []
    if chain and chain.get("status") == "active":
        cursor = chain.get("cursor", 0)
        slugs = chain.get("slugs", [])
        active = active_info(goals)
        if cursor >= len(slugs):
            problems.append(("cursor past end but chain still active",
                             lambda: _complete_chain(goals, chain, slugs[-1])))
        else:
            cur_slug = slugs[cursor]
            cur_state = load_state(goals, cur_slug)
            # Symptom C: previous link done but cursor not advanced
            if cur_state and cur_state.get("status") == "done" and \
                    cur_state.get("last_judge_verdict") == "approve":
                problems.append((
                    f"'{cur_slug}' at cursor is done+approved but cursor not advanced",
                    lambda c=chain, s=cur_slug: print(_advance_chain(goals, c, s))))
            # Symptom B: cursor advanced but next state.json missing
            elif cur_state is None:
                problems.append((
                    f"cursor points at '{cur_slug}' but its state.json is missing",
                    lambda c=chain, s=cur_slug: _activate(
                        goals, s, chain_name=c["name"], chain_step=cursor + 1)))
            # Symptom A: active.json stale
            elif active != cur_slug and cur_state.get("status") == "active":
                problems.append((
                    f"active.json points at '{active}' but chain cursor is at "
                    f"'{cur_slug}'",
                    lambda c=chain, s=cur_slug: write_json(
                        goals / "active.json",
                        {"slug": s, "activated_at": now_iso(), "chain": c["name"]}),
                    cur_slug))
            # Symptom D: missing link_approval for prior approved links
            approvals = {a.get("slug") for a in chain.get("link_approvals", [])}
            for prior in slugs[:cursor]:
                if prior not in approvals:
                    st = load_state(goals, prior) or {}
                    when = st.get("approved_at") or now_iso()
                    problems.append((
                        f"link_approvals missing entry for approved '{prior}'",
                        lambda c=chain, p=prior, w=when: (
                            c.setdefault("link_approvals", []).append(
                                {"slug": p, "approved_at": w}),
                            write_json(goals / "chain.json", c)),
                        prior))
    slug = active_info(goals)
    if slug and load_state(goals, slug) is None:
        problems.append((f"active.json points at '{slug}' but state.json missing",
                         lambda: write_json(goals / "active.json", {
                             "slug": None, "ended_at": now_iso(),
                             "ended_reason": "cleared", "previous_slug": slug}),
                         slug))
    if not problems:
        print("OK — no inconsistencies detected.")
        return 0
    for entry in problems:
        desc, repair = entry[0], entry[1]
        subject = entry[2] if len(entry) > 2 else None
        print(f"PROBLEM: {desc}")
        if args.fix:
            repair()
            print("  fixed.")
            # A repair is a state change like any other. Some repairs route
            # through _activate/_advance_chain and emit their own event; the
            # rest would otherwise be the one class of gk write no one outside
            # could see.
            if subject:
                emit_goal_event(goals, subject, "goal.repaired",
                                f"gk doctor repaired: {desc}",
                                extra={"problem": desc})
    if not args.fix:
        print("\nRun `gk doctor --fix` to repair.")
        return 1
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Mission layer (supervisor state)
# ─────────────────────────────────────────────────────────────────────────────

MISSION_FILES = ("mission.json", "mission-log.md", "mission-completed.md")


def claude_dir(goals: Path) -> Path:
    return goals.parent


def read_mission(goals: Path) -> Optional[dict]:
    return read_json(claude_dir(goals) / "mission.json")


def write_mission(goals: Path, mission: dict) -> None:
    write_json(claude_dir(goals) / "mission.json", mission)


def mission_charter(goals: Path) -> Optional[str]:
    path = claude_dir(goals) / "mission.md"
    return path.read_text() if path.is_file() else None


def mission_name_from_md(text: str) -> str:
    for line in text.splitlines():
        m = re.match(r"^#\s+(?:Mission:\s*)?(.+)$", line.strip())
        if m:
            return m.group(1).strip()
    return "unnamed-mission"


def append_mission_log(goals: Path, title: str, body: str) -> None:
    log = claude_dir(goals) / "mission-log.md"
    entry = f"\n## {now_iso()} — {title}\n"
    if body.strip():
        entry += body.rstrip() + "\n"
    with log.open("a") as f:
        f.write(entry)


def locate_prior_goal(goals: Path) -> Optional[dict]:
    """Most-recently-ended goal: {slug, dir, state, log_text} or None."""
    def bundle(slug: str, d: Path) -> Optional[dict]:
        state = read_json(d / "state.json")
        if state is None:
            return None
        log_path = d / "log.md"
        return {"slug": slug, "dir": d, "state": state,
                "log_text": log_path.read_text() if log_path.is_file() else ""}

    active = read_json(goals / "active.json") or {}
    prev = active.get("previous_slug")
    if prev:
        if (goals / prev).is_dir():
            b = bundle(prev, goals / prev)
            if b:
                return b
        arch = sorted((goals / "_archive").glob(f"{prev}-*")) \
            if (goals / "_archive").is_dir() else []
        if arch:
            b = bundle(prev, arch[-1])
            if b:
                return b
    arch_dirs = sorted(d for d in (goals / "_archive").iterdir() if d.is_dir()) \
        if (goals / "_archive").is_dir() else []
    if arch_dirs:
        d = arch_dirs[-1]
        slug = re.sub(r"-\d{8}-\d{6}$", "", d.name)
        b = bundle(slug, d)
        if b:
            return b
    done = []
    for d in goals.iterdir():
        if d.is_dir() and d.name not in ("_archive", "shared"):
            st = read_json(d / "state.json")
            if st and st.get("status") == "done":
                done.append((st.get("approved_at") or st.get("started_at") or "", d))
    if done:
        d = max(done)[1]
        return bundle(d.name, d)
    return None


def _goal_in_flight(goals: Path) -> Optional[str]:
    """Slug of a goal whose status blocks the supervisor, else None."""
    slug = active_info(goals)
    if not slug:
        return None
    st = load_state(goals, slug) or {}
    return slug if st.get("status") in ("active", "paused", "needs_human") else None


def cmd_mission_init(args) -> int:
    # create=True: a mission legitimately starts before any goal has ever
    # been activated, so .claude/goals/ may not exist yet.
    goals = find_goals_dir(create=True)
    charter = mission_charter(goals)
    if charter is None:
        die(f"no mission charter at {claude_dir(goals) / 'mission.md'}. "
            "The supervisor requires a user-authored charter — it does not "
            "auto-draft missions.")
    blocking = _goal_in_flight(goals)
    if blocking:
        die(f"goal '{blocking}' is in flight — supervisor layer refuses while "
            "a goal is active/paused/needs_human.")
    mission = read_mission(goals)
    if mission:
        print(f"Mission '{mission.get('name')}' already initialized "
              f"(status: {mission.get('status')}).")
        return 0
    mission = {
        "name": mission_name_from_md(charter),
        "status": "active",
        "started_at": now_iso(),
        "goals_completed": [],
        "supervisor_verdicts": [],
    }
    write_mission(goals, mission)
    append_mission_log(goals, "mission initialized",
                       f"Mission: {mission['name']}")
    emit_mission_event(goals, mission, "mission.initialized",
                       f"mission '{mission['name']}' initialized",
                       extra={"from_status": None, "to_status": "active"})
    print(f"Mission '{mission['name']}' initialized.")
    return 0


def cmd_mission_status(args) -> int:
    goals = find_goals_dir()
    mission = read_mission(goals)
    if not mission:
        print("No mission. Author .claude/mission.md, then run gk mission-init.")
        return 0
    print(f"Mission:     {mission.get('name')}")
    print(f"Status:      {mission.get('status')}")
    print(f"Started:     {mission.get('started_at')}")
    done = mission.get("goals_completed", [])
    print(f"Goals done:  {len(done)}"
          + (f"  ({', '.join(g['slug'] for g in done)})" if done else ""))
    verdicts = mission.get("supervisor_verdicts", [])
    if verdicts:
        last = verdicts[-1]
        print(f"Last verdict: {last.get('verdict')} "
              f"(prior goal: {last.get('prior_slug')}, at {last.get('at')})")
    return 0


SUPERVISOR_TASK = """\
# Your task

You are the mission supervisor. The user's mission is described above.
One goal has just completed (or the mission is just starting). Your job:
decide what happens next.

You have three legal outputs:

PROCEED — the mission is still active and the next goal can be named.
  Output a one-sentence objective for the next goal. Reference what the
  prior goal produced and how it shapes this one. The objective will be
  fed to /goalkeeper:goal-prep, which drafts a full contract for USER
  REVIEW — proceed never activates anything by itself.

DONE — the mission's success condition is satisfied. Cite the specific
  evidence in the prior goal(s) that demonstrates each part of the
  success condition.

ESCALATE — you cannot decide. Either the prior goal's output is
  ambiguous, the mission charter is internally inconsistent, the
  success condition isn't observable from the artifacts, or you've
  hit a constraint that requires human judgment. Explain in 3-5
  sentences exactly what decision needs human input.

Escalate rather than proceed when: the prior goal ended in needs_human;
the prior goal touched files the charter's Constraints mark off-limits;
the success condition references a metric no goal has produced evidence
for; or no shape in the charter's "Legal next-goal shapes" fits what is
needed next.

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
"""


def cmd_mission_brief(args) -> int:
    goals = find_goals_dir()
    charter = mission_charter(goals)
    if charter is None:
        die("no .claude/mission.md — run gk mission-init guidance first")
    mission = read_mission(goals)
    if mission is None:
        die("mission not initialized — run gk mission-init")
    blocking = _goal_in_flight(goals)
    if blocking:
        die(f"goal '{blocking}' is in flight — supervisor refuses.")
    root = project_root(goals)
    prior = locate_prior_goal(goals)

    out = []
    out.append("You are the mission supervisor for a goalkeeper mission. "
               "Fresh context — you have not seen any executing agent's "
               "reasoning. Review the artifacts only.\n")
    out.append("# Mission charter (user-authored — verbatim)\n")
    out.append(charter)
    out.append("\n# Mission progress\n")
    out.append(json.dumps({
        "goals_completed": mission.get("goals_completed", []),
        "prior_verdicts": mission.get("supervisor_verdicts", []),
    }, indent=2))
    if prior:
        out.append(f"\n# Prior goal: {prior['slug']}\n")
        out.append("## state.json\n")
        out.append(json.dumps(prior["state"], indent=2))
        out.append("\n## Progress log (compacted)\n")
        out.append(compact_log(prior["log_text"]) if prior["log_text"]
                   else "(no log)")
    else:
        out.append("\n# Prior goal\n")
        out.append("None — this is the mission's first supervisor invocation. "
                   "Propose the first goal from the charter's "
                   "\"Legal next-goal shapes\" section.")
    head = _git(root, "rev-parse", "HEAD")
    porcelain = _git(root, "status", "--porcelain") or ""
    out.append("\n# Repo state\n")
    out.append(f"HEAD: {(head or 'no-git').strip()[:9]}")
    dirty_lines = porcelain.splitlines()[:20]
    out.append("Dirty paths (first 20):\n" +
               ("\n".join(dirty_lines) if dirty_lines else "(clean)"))
    out.append("")
    out.append(SUPERVISOR_TASK)
    print("\n".join(out))
    return 0


def cmd_mission_verdict(args) -> int:
    goals = find_goals_dir()
    mission = read_mission(goals)
    if mission is None:
        die("mission not initialized — run gk mission-init")
    if mission.get("status") != "active":
        die(f"mission status is '{mission.get('status')}' — verdicts only "
            "apply to an active mission.")
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    reasoning = _extract_section(raw, "REASONING") or "(none provided)"
    prior = locate_prior_goal(goals)
    prior_slug = prior["slug"] if prior else None

    verdicts = mission.setdefault("supervisor_verdicts", [])
    # One invocation per goal-completion — except a resolved escalation:
    # after mission-resume, the supervisor legitimately re-runs against the
    # same prior goal (the status gate above blocks un-resumed escalations).
    if verdicts and verdicts[-1].get("prior_slug") == prior_slug \
            and verdicts[-1].get("verdict") != "escalate":
        die(f"a supervisor verdict for prior goal '{prior_slug}' is already "
            f"recorded ({verdicts[-1].get('verdict')} at "
            f"{verdicts[-1].get('at')}). One invocation per goal-completion — "
            "complete another goal first.")

    entry = {"at": now_iso(), "prior_slug": prior_slug, "verdict": args.decision}

    if args.decision == "proceed":
        next_obj = _extract_section(raw, "NEXT_OBJECTIVE")
        if not next_obj:
            die("proceed verdict requires a NEXT_OBJECTIVE section on stdin")
        entry["next_objective"] = next_obj
        append_mission_log(goals, "supervisor verdict: proceed",
                           f"Prior goal: {prior_slug or '(none — first invocation)'}\n"
                           f"Reasoning: {reasoning}\n"
                           f"Proposed next objective: {next_obj}")
    elif args.decision == "done":
        evidence = _extract_section(raw, "DONE_EVIDENCE")
        if not evidence:
            die("done verdict requires a DONE_EVIDENCE section on stdin")
        append_mission_log(goals, "supervisor verdict: done",
                           f"Mission: {mission.get('name')}\n"
                           f"Reasoning: {reasoning}\nEvidence:\n{evidence}")
        mission["status"] = "done"
        mission["completed_at"] = now_iso()
    else:  # escalate
        escalation = _extract_section(raw, "ESCALATION")
        if not escalation:
            die("escalate verdict requires an ESCALATION section on stdin")
        entry["escalation"] = escalation
        append_mission_log(goals, "supervisor verdict: escalate",
                           f"Prior goal: {prior_slug or '(none)'}\n"
                           f"Reasoning: {reasoning}\n"
                           f"Required input: {escalation}")
        mission["status"] = "escalated"

    if prior:
        completed = mission.setdefault("goals_completed", [])
        if not any(g.get("slug") == prior_slug for g in completed):
            state = prior["state"]
            completed.append({
                "slug": prior_slug,
                "result": "approved"
                if state.get("last_judge_verdict") == "approve" else "cleared",
                "rejection_count": state.get("rejection_count", 0),
                "ended_at": state.get("approved_at") or now_iso(),
            })
        with (prior["dir"] / "log.md").open("a") as f:
            f.write(f"\n## {now_iso()} — supervisor verdict\n"
                    f"Mission `{mission.get('name')}` supervisor verdict on "
                    f"this goal: {args.decision}.\n"
                    f"See `.claude/mission-log.md` for full reasoning.\n")

    verdicts.append(entry)
    write_mission(goals, mission)
    emit_mission_event(
        goals, mission, "mission.verdict",
        f"supervisor verdict '{args.decision}' on mission "
        f"'{mission.get('name')}'",
        extra={"verdict": args.decision, "prior_slug": prior_slug,
               "to_status": mission.get("status"),
               "reasoning": reasoning[:EMIT_TEXT_CAP],
               "next_objective": entry.get("next_objective"),
               "escalation": entry.get("escalation")},
        # Same signal type as a parked goal, deliberately: the condition is the
        # same shape — work that cannot proceed without a named person — and gk
        # publishing two vocabularies for one condition would make it harder to
        # read, not more precise.
        escalate=({
            "signal_type": "hard_dependency_broken",
            "severity": "attention",
            "reason": f"mission '{mission.get('name')}' escalated to a person: "
                      f"{entry.get('escalation')}",
        } if args.decision == "escalate" else None))

    if args.decision == "done":
        snapshot = (f"# Mission completed: {mission.get('name')}\n\n"
                    f"Completed at: {mission['completed_at']}\n\n"
                    f"## Final mission.json\n\n```json\n"
                    f"{json.dumps(mission, indent=2)}\n```\n\n"
                    f"## Supervisor reasoning\n\n{reasoning}\n\n"
                    f"## Evidence\n\n"
                    f"{_extract_section(raw, 'DONE_EVIDENCE')}\n\n"
                    f"## Original charter (copy)\n\n{mission_charter(goals)}\n")
        (claude_dir(goals) / "mission-completed.md").write_text(snapshot)
        print("DONE")
        print(f"Mission '{mission.get('name')}' complete. "
              f"Snapshot: {claude_dir(goals) / 'mission-completed.md'}")
    elif args.decision == "proceed":
        print("PROCEED")
        print(f"NEXT_OBJECTIVE: {entry['next_objective']}")
    else:
        print("ESCALATE")
        print(entry["escalation"])
    return 0


def cmd_mission_resume(args) -> int:
    """Escalated → active: the user has provided the required input."""
    goals = find_goals_dir()
    mission = read_mission(goals)
    if mission is None:
        die("mission not initialized — run gk mission-init")
    if mission.get("status") != "escalated":
        die(f"mission status is '{mission.get('status')}' — mission-resume "
            "only applies to an escalated mission.")
    mission["status"] = "active"
    write_mission(goals, mission)
    note = (args.note or "").strip()
    append_mission_log(goals, "escalation resolved",
                       "Resumed by user."
                       + (f"\nResolution: {note}" if note else ""))
    emit_mission_event(
        goals, mission, "mission.resumed",
        f"mission '{mission.get('name')}' resumed after escalation",
        extra={"from_status": "escalated", "to_status": "active",
               "resolution": note or None},
        stand_down=f"mission '{mission.get('name')}' escalation resolved")
    print(f"Mission '{mission.get('name')}' resumed (escalation resolved). "
          "Re-run the supervisor: gk mission-brief → spawn → gk mission-verdict.")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# hook-guard — PreToolUse enforcement of goalkeeper invariants
# ─────────────────────────────────────────────────────────────────────────────

def cmd_hook_guard(_args) -> int:
    """Block direct Edit/Write to an active goal's contract/log/state files.

    Exit 0 = allow, exit 2 = block (stderr shown to the model). Fails open on
    any error — this hook must never brick unrelated edits.
    """
    try:
        payload = json.load(sys.stdin)
        tool_input = payload.get("tool_input") or {}
        path_str = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not path_str:
            return 0
        norm = str(Path(path_str))
        marker = f"{os.sep}.claude{os.sep}goals{os.sep}"
        idx = norm.find(marker)
        if idx == -1:
            # Mission state files live at .claude/ root and are gk-managed
            # while the mission is live (mission.md, the user's charter,
            # is never blocked).
            cmarker = f"{os.sep}.claude{os.sep}"
            cidx = norm.find(cmarker)
            if cidx == -1:
                return 0
            crel = norm[cidx + len(cmarker):]
            if crel in MISSION_FILES:
                mission = read_json(
                    Path(norm[: cidx + len(cmarker)].rstrip(os.sep))
                    / "mission.json") or {}
                if mission.get("status") in ("active", "escalated"):
                    print(
                        f"goalkeeper hook-guard: '{crel}' belongs to the live "
                        f"mission '{mission.get('name')}' and is managed by "
                        f"the gk CLI — use `gk mission-init` / "
                        f"`gk mission-verdict` (scripts/gk.py in the "
                        f"goalkeeper plugin). The charter (mission.md) stays "
                        f"user-editable.",
                        file=sys.stderr,
                    )
                    return 2
            return 0
        goals = Path(norm[: idx + len(marker)].rstrip(os.sep))
        rel = norm[idx + len(marker):]
        if rel.startswith("_archive" + os.sep) or rel.startswith("shared" + os.sep):
            return 0
        # active.json/chain.json are gk-owned whenever any goal is live —
        # the touched goal's own state decides everything else, so parked
        # (needs_human) goals stay protected after the slot is freed.
        if rel in ("active.json", "chain.json"):
            live = active_info(goals) or next(
                (s for s, _ in _parked_goals(goals)), None)
            if not live:
                return 0
            slug = live
        else:
            slug = rel.split(os.sep)[0]
            state = read_json(goals / slug / "state.json") or {}
            if state.get("status") not in ("active", "paused", "needs_human"):
                return 0
        protected = {
            f"{slug}{os.sep}contract.md",
            f"{slug}{os.sep}log.md",
            f"{slug}{os.sep}state.json",
            f"{slug}{os.sep}judge-token.json",
            f"{slug}{os.sep}receipt.json",
            "active.json",
            "chain.json",
        }
        if rel in protected:
            print(
                f"goalkeeper hook-guard: '{rel}' belongs to the live goal "
                f"'{slug}' and is managed by the gk CLI — direct edits break "
                f"the audit trail. Use `gk checkpoint`, `gk verdict`, "
                f"`gk pause/park/resume/clear` (scripts/gk.py in the "
                f"goalkeeper plugin) instead. If the contract itself is "
                f"wrong, /goal-clear and re-prep — contracts are immutable "
                f"mid-run.",
                file=sys.stderr,
            )
            return 2
        return 0
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(prog="gk", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("activate")
    sp.add_argument("slug")
    sp.add_argument("--chain")
    sp.add_argument("--chain-step", type=int)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_activate)

    sp = sub.add_parser("baseline")
    sp.add_argument("slug")
    sp.add_argument("--paths")
    sp.set_defaults(fn=cmd_baseline)

    sp = sub.add_parser("checkpoint")
    sp.add_argument("slug")
    sp.add_argument("--message")
    sp.set_defaults(fn=cmd_checkpoint)

    sp = sub.add_parser("validate")
    sp.add_argument("slug")
    sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("judge-brief")
    sp.add_argument("slug")
    sp.add_argument("--executor-summary")
    sp.add_argument("--mode", choices=["subagent", "inline"],
                    help="judge mode actually being run; recorded into the "
                         "minted token (default: the contract's judge_mode)")
    sp.set_defaults(fn=cmd_judge_brief)

    sp = sub.add_parser("verdict")
    sp.add_argument("slug")
    sp.add_argument("decision", choices=["approve", "reject"])
    sp.set_defaults(fn=cmd_verdict)

    sp = sub.add_parser("receipt")
    sp.add_argument("slug")
    sp.set_defaults(fn=cmd_receipt)

    sp = sub.add_parser("chain-start")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_chain_start)

    sp = sub.add_parser("advance")
    sp.add_argument("--fix", action="store_true")
    sp.set_defaults(fn=cmd_advance)

    sp = sub.add_parser("pause")
    sp.set_defaults(fn=cmd_pause)

    sp = sub.add_parser("park")
    sp.add_argument("slug", nargs="?")
    sp.add_argument("--needs", required=True,
                    help="exactly what a person must provide "
                         "(credential, consent, decision)")
    sp.set_defaults(fn=cmd_park)

    sp = sub.add_parser("resume")
    sp.add_argument("slug", nargs="?")
    sp.add_argument("--reset-rejections", action="store_true")
    sp.add_argument("--keep-count", action="store_true")
    sp.set_defaults(fn=cmd_resume)

    sp = sub.add_parser("clear")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(fn=cmd_clear)

    sp = sub.add_parser("log")
    sp.add_argument("slug")
    sp.add_argument("--compact", action="store_true")
    sp.add_argument("--checkpoints", type=int, default=5)
    sp.set_defaults(fn=cmd_log)

    sp = sub.add_parser("doctor")
    sp.add_argument("--fix", action="store_true")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("mission-init")
    sp.set_defaults(fn=cmd_mission_init)

    sp = sub.add_parser("mission-status")
    sp.set_defaults(fn=cmd_mission_status)

    sp = sub.add_parser("mission-brief")
    sp.set_defaults(fn=cmd_mission_brief)

    sp = sub.add_parser("mission-verdict")
    sp.add_argument("decision", choices=["proceed", "done", "escalate"])
    sp.set_defaults(fn=cmd_mission_verdict)

    sp = sub.add_parser("mission-resume")
    sp.add_argument("--note")
    sp.set_defaults(fn=cmd_mission_resume)

    sp = sub.add_parser("hook-guard")
    sp.set_defaults(fn=cmd_hook_guard)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
