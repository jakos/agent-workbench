# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Opt-in workflow transitions and audit evidence."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import approval_witness
import policy
import role_contracts
import signed_approval
from rules import witness_command, witness_write

PHASES = {
    "new",
    "planned",
    "approved",
    "baseline",
    "implementing",
    "checking",
    "reviewing",
    "review-approved",
    "blocked",
    "escalated",
    "done",
}
EDGES = {
    "new": {"planned"},
    "planned": {"approved", "blocked"},
    "approved": {"baseline"},
    "baseline": {"implementing"},
    "implementing": {"checking", "blocked"},
    "checking": {"reviewing"},
    "reviewing": {"implementing", "review-approved", "escalated"},
    "review-approved": {"done"},
    "blocked": set(),
    "escalated": {"implementing"},
    "done": set(),
}
FIELDS = {
    "phase",
    "round",
    "base",
    "branch",
    "worktree",
    "plan-approved",
    "last-verdict",
}
SCHEMA = "phase-gate/1"


class Refusal(ValueError):
    """A protected operation did not start; resumption remains possible."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise Refusal(f"cannot read {path.name}") from exc


def status(task: Path) -> tuple[str, dict]:
    text = read(task / "status.md")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", task.name):
        raise Refusal("invalid task identity")
    if not text.startswith(f"# Status: {task.name}\n"):
        raise Refusal("status task identity mismatch")
    if text.count("\n## History\n") != 1 or text.count("\n## Log\n") != 1:
        raise Refusal("status requires unique History and Log sections")
    header, rest = text.split("\n## History\n")
    if "\n## Log\n" not in rest or "\n## " in header:
        raise Refusal("invalid status sections")
    fields = {}
    for line in header.splitlines()[1:]:
        if not line:
            continue
        match = re.fullmatch(r"- ([a-z-]+): (\S.*)", line)
        if not match or match[1] in fields or match[1] not in FIELDS:
            raise Refusal("invalid or duplicate status field")
        fields[match[1]] = match[2]
    if set(fields) != FIELDS:
        raise Refusal("missing status field")
    if fields["phase"] not in PHASES or not re.fullmatch(
        r"0|[1-9]\d*", fields["round"]
    ):
        raise Refusal("invalid status phase or round")
    if fields["last-verdict"] not in ("none", "APPROVED", "CHANGES_REQUESTED"):
        raise Refusal("invalid status verdict")
    if not re.fullmatch(
        r"no|yes \((user|policy|--auto), [^\n]+\)", fields["plan-approved"]
    ):
        raise Refusal("invalid status approval")
    fields["round"] = int(fields["round"])
    if (
        fields["phase"] in ("new", "planned", "approved", "baseline")
        and fields["round"] != 0
    ) or (
        fields["phase"]
        in (
            "implementing",
            "checking",
            "reviewing",
            "review-approved",
            "escalated",
            "done",
        )
        and fields["round"] < 1
    ):
        raise Refusal("phase and round contradict one another")
    expected = f".work/{task.name}/worktree"
    if fields["worktree"].replace("\\", "/") not in ("none", expected):
        raise Refusal("ambiguous task worktree")
    return text, fields


def initialize(task: Path, base: str, *, home: Path | None = None) -> None:
    """Create the initial unapproved header through the same audit boundary."""
    task = task.resolve()
    if (
        task.parent.name != ".work"
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", task.name)
        or not base
        or any(c.isspace() for c in base)
    ):
        raise Refusal("invalid task identity or base")
    loaded = policy.effective(policy.load(task.parent.parent, home or Path.home()))
    task.mkdir(exist_ok=True)
    with locked(task):
        entries = log_entries(task)
        path = task / "status.md"
        text = f"# Status: {task.name}\n- phase: new\n- round: 0\n- base: {base}\n- branch: task/{task.name}\n- worktree: none\n- plan-approved: no\n- last-verdict: none\n## History\n- initialized\n## Log\n"
        if path.exists() and read(path) != text:
            raise Refusal("initialize cannot replace an existing status")
        event = {
            "schema": SCHEMA,
            "task": task.name,
            "run": None,
            "time": datetime.now(UTC).isoformat(),
            "action": "initialize",
            "before": None if not path.exists() else {"phase": "new", "round": 0},
            "after": {"phase": "new", "round": 0},
            "allowed": True,
            "reason": "allowed" if not entries else "repeated initialization",
            "policy": digest(json.dumps(loaded, sort_keys=True).encode()),
            "source": "policy",
            "plan": digest(b""),
            "attempt": digest(json.dumps([base, f"task/{task.name}", []]).encode()),
            "ceiling": 0,
        }
        if loaded["mode"] != "legacy":
            append(task, event)
        publish(path, text)


def attempt(text: str, fields: dict, task: Path | None = None) -> str:
    return digest(json.dumps(attempt_inputs(text, fields, task)).encode())


def attempt_inputs(text: str, fields: dict, task: Path | None = None) -> list:
    history = text.split("\n## History\n")[1].split("\n## Log\n")[0]
    retries = []
    for line in history.splitlines():
        match = re.search(r"round reset to 0 for attempt (\d+) of epic ([\w-]+) ", line)
        if match and "authorized by the user" in line:
            if task is not None:
                archive = (
                    task.parent
                    / "_epics"
                    / match[2]
                    / "attempts"
                    / task.name
                    / f"attempt-{match[1]}"
                    / "spend.md"
                )
                if not archive.is_file():
                    raise Refusal("retry has no archived spend evidence")
            retries.append(f"{match[2]}/{match[1]}")
    return [fields["base"], fields["branch"], retries]


@contextmanager
def locked(task: Path) -> Iterator[None]:
    """OS locks release after crashes. Never unlink the shared lock inode."""
    with (task.parent / f".gate-lock-{task.name}").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def log_entries(task: Path) -> list[dict]:
    path = task / "gates.jsonl"
    if not path.exists():
        return []
    return parse_log(path.read_bytes(), task.name)


def resolved_attempt(
    text: str,
    fields: dict,
    task: Path,
    entries: list[dict],
    *,
    pending_base: str | None = None,
) -> str:
    """Resolve raw identity through validated links, without reading the log again."""
    raw = attempt(text, fields, task)
    plan = digest(read(task / "plan.md").encode("utf-8"))
    links = [e for e in entries if e.get("rebase") and e["plan"] == plan]
    seen = set()
    for event in reversed(links):
        if event["attempt"] in seen:
            continue
        seen.add(event["attempt"])
        link = event["rebase"]
        if raw == link["new_attempt"]:
            return event["attempt"]
        if raw == link["old_attempt"]:
            if pending_base == link["new_base"]:
                history = text.split("\n## History\n")[1].split("\n## Log\n")[0]
                if rebase_history(event) in history.splitlines():
                    raise Refusal("completed base change was edited outside the gate")
                return event["attempt"]
            raise Refusal("base change publication is pending; repeat exact rebase")
        if any(
            raw in (e["rebase"]["old_attempt"], e["rebase"]["new_attempt"])
            for e in links
            if e["attempt"] == event["attempt"]
        ):
            raise Refusal("base does not match latest controlled change")
    return raw


def record_digest(task: Path) -> str:
    """Digest a validated canonical snapshot, stable across repeated finish requests.

    The caller holds the task lock. The complete log is still validated and retained.
    """
    entries = log_entries(task)
    text, fields = status(task)
    plan_hash = digest(read(task / "plan.md").encode("utf-8"))
    attempt_hash = resolved_attempt(text, fields, task, entries)
    for index, event in enumerate(entries):
        if (
            event["allowed"]
            and event["action"] == "finish"
            and event["plan"] == plan_hash
            and event["attempt"] == attempt_hash
            and event["before"]
            == {"phase": "review-approved", "round": fields["round"]}
        ):
            entries = entries[: index + 1]
            break
    return digest(
        json.dumps(
            entries, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode("utf-8")
    )


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal("duplicate gate-log key")
        result[key] = value
    return result


def parse_log(raw: bytes, task_id: str) -> list[dict]:
    if raw and not raw.endswith(b"\n"):
        raise Refusal("interrupted gate log; restore complete evidence before resuming")
    entries = []
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        if index + 1 < len(lines):
            try:
                following = json.loads(
                    lines[index + 1], object_pairs_hook=unique_object
                )
            except (ValueError, UnicodeError):
                following = {}
            if (
                isinstance(following, dict)
                and following.get("action") == "recover"
                and following.get("reason") == "interrupted:" + digest(line)
            ):
                continue  # This fragment never supplies authority, even if valid JSON.
        try:
            event = json.loads(line, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError) as exc:
            raise Refusal("malformed gate log") from exc
        required = {
            "schema",
            "task",
            "run",
            "time",
            "action",
            "before",
            "after",
            "allowed",
            "reason",
            "policy",
            "source",
            "plan",
            "attempt",
            "ceiling",
        }
        if (
            not isinstance(event, dict)
            or set(event)
            not in (required, required | {"rebase"}, required | {"authorization"})
            or event["schema"] != SCHEMA
            or event["task"] != task_id
            or type(event["allowed"]) is not bool
            or event["source"] not in ("user", "policy", "--auto")
            or not all(
                isinstance(event[k], str)
                for k in ("time", "action", "reason", "policy", "plan", "attempt")
            )
            or not all(
                re.fullmatch(r"[a-f0-9]{64}", event[k])
                for k in ("plan", "attempt", "policy")
            )
            or type(event["ceiling"]) is not int
            or event["ceiling"] < 0
            or event["run"] is not None
            and (
                not isinstance(event["run"], str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", event["run"])
            )
        ):
            raise Refusal("invalid gate evidence")
        try:
            stamp = datetime.fromisoformat(event["time"])
            if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
                raise ValueError("not UTC")
        except ValueError as exc:
            raise Refusal("invalid gate UTC time") from exc
        for state in (event["before"], event["after"]):
            if state is not None and (
                not isinstance(state, dict)
                or set(state) != {"phase", "round"}
                or not isinstance(state["phase"], str)
                or state["phase"] not in PHASES
                or type(state["round"]) is not int
                or state["round"] < 0
            ):
                raise Refusal("invalid logged state")
        if "authorization" in event:
            evidence = event["authorization"]
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"channel", "reference"}
                or evidence["channel"]
                not in ("user-prompt", "terminal", "ssh-signature")
                or not isinstance(evidence["reference"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", evidence["reference"])
                or event["source"] != "user"
                or not event["allowed"]
                or event["action"] not in ("approve", "grant")
                or any(
                    e.get("authorization", {}).get("reference") == evidence["reference"]
                    for e in entries
                )
            ):
                raise Refusal("invalid authorization reference")
        if "rebase" in event:
            link = event["rebase"]
            keys = {
                "schema",
                "old_base",
                "new_base",
                "old_attempt",
                "new_attempt",
                "branch",
                "retries",
            }
            if (
                not isinstance(link, dict)
                or set(link) != keys
                or link["schema"] != "base-change/1"
                or event["action"] != "rebase"
                or not event["allowed"]
                or event["run"] is None
                or event["before"] != {"phase": "approved", "round": 0}
                or event["after"] != event["before"]
                or not all(
                    isinstance(link[k], str)
                    and link[k]
                    and not any(c.isspace() for c in link[k])
                    for k in ("old_base", "new_base", "branch")
                )
                or link["old_base"] == link["new_base"]
                or not isinstance(link["retries"], list)
                or not all(
                    isinstance(r, str) and re.fullmatch(r"[\w-]+/\d+", r)
                    for r in link["retries"]
                )
                or any(
                    link[f"{side}_attempt"]
                    != digest(
                        json.dumps(
                            [link[f"{side}_base"], link["branch"], link["retries"]]
                        ).encode()
                    )
                    for side in ("old", "new")
                )
            ):
                raise Refusal("invalid base-change evidence")
            prior = [
                e
                for e in entries
                if e["allowed"]
                and e["plan"] == event["plan"]
                and e["attempt"] == event["attempt"]
            ]
            approvals = [e for e in prior if e["action"] == "approve"]
            changes = [e for e in prior if e.get("rebase")]
            previous = (
                changes[-1]["rebase"]["new_attempt"] if changes else event["attempt"]
            )
            started = any(
                e["action"] == "baseline"
                or e["action"] == "transition"
                and e["after"] == {"phase": "baseline", "round": 0}
                for e in prior
            )
            if (
                not approvals
                or started
                or previous != link["old_attempt"]
                or event["source"] != approvals[-1]["source"]
            ):
                raise Refusal("disconnected base-change evidence")
            if changes and (
                changes[-1]["run"] != event["run"]
                or any(
                    link[k] != changes[-1]["rebase"][k] for k in ("branch", "retries")
                )
            ):
                raise Refusal("base change changed task identity")
        entries.append(event)
    return entries


def recover(task: Path, *, human: bool) -> None:
    """Preserve incomplete bytes, append a delimiter and a fragment tombstone."""
    if not human:
        raise Refusal("recovery requires separate-terminal human confirmation")
    with locked(task):
        raw = (task / "gates.jsonl").read_bytes()
        if not raw or raw.endswith(b"\n"):
            raise Refusal("no incomplete final log entry")
        prefix, _, fragment = raw.rpartition(b"\n")
        parse_log(prefix + b"\n" if prefix else b"", task.name)
        event = {
            "schema": SCHEMA,
            "task": task.name,
            "run": None,
            "time": datetime.now(UTC).isoformat(),
            "action": "recover",
            "before": None,
            "after": None,
            "allowed": True,
            "reason": "interrupted:" + digest(fragment),
            "policy": digest(b""),
            "source": "user",
            "plan": digest(b""),
            "attempt": digest(b""),
            "ceiling": 0,
        }
        with (task / "gates.jsonl").open("ab", buffering=0) as handle:
            data = b"\n" + (json.dumps(event, sort_keys=True) + "\n").encode()
            if handle.write(data) != len(data):
                raise Refusal("incomplete recovery write")
            os.fsync(handle.fileno())


def append(task: Path, event: dict) -> None:
    data = (json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    with (task / "gates.jsonl").open("ab", buffering=0) as handle:
        if handle.write(data) != len(data):
            raise Refusal("incomplete gate log write")
        os.fsync(handle.fileno())


def checks(
    task: Path, number: int, commands: list[str]
) -> role_contracts.CheckEvidence:
    return role_contracts.parse_checks(
        read(task / f"checks-{number}.md"), round_number=number, commands=commands
    )


def review(task: Path, number: int, plan: str, commands: list[str]) -> tuple[str, set]:
    baseline = checks(task, 0, commands)
    current = checks(task, number, commands)
    text = read(task / f"review-{number}.md")
    validated = role_contracts.validate_review(
        text, round_number=number, plan=plan, current=current, baseline=baseline
    )
    if not validated.ok:
        raise Refusal("invalid review: " + "; ".join(validated.reasons))
    verdict = re.search(r"^## Verdict: (\w+)$", text, re.MULTILINE)[1]
    introduced = role_contracts.classify_checks(current, baseline)[1]
    failures = {"command:" + json.dumps(command) for command in introduced}
    failures.update(re.findall(r"^\| (AC\d+) \| FAIL \|", text, re.MULTILINE))
    return verdict, failures


def stopped(
    task: Path, number: int, plan: str, commands: list[str], ceiling: int
) -> str | None:
    verdict, failures = review(task, number, plan, commands)
    if verdict == "APPROVED":
        return "APPROVED"
    if (
        number >= 2
        and failures
        and failures == review(task, number - 1, plan, commands)[1]
    ):
        return "no progress"
    if number >= ceiling:
        return "round cap"
    return None


def run_context(
    task: Path, entries: list[dict] | None = None, *, pending_base: str | None = None
) -> str | None:
    """Resolve membership from persisted run tables, never a model-supplied flag."""
    import autopilot

    matches = []
    for path in sorted((task.parent / "_autopilot").glob("*/run.md")):
        rows = re.findall(r"^\| \d+ \| ([a-z0-9-]+) \|", read(path), re.MULTILINE)
        if task.name in rows:
            fields = autopilot.header_fields(autopilot.sections(read(path))[""])
            if fields.get("state") != "done":
                if fields.get("state") not in ("ready", "running"):
                    raise Refusal("autopilot is paused, stopped or not ready")
                matches.append(path.parent)
    if len(matches) > 1:
        raise Refusal("ambiguous autopilot membership")
    if not matches:
        return None
    try:
        run = autopilot.load_run(str(matches[0]), True)
    except autopilot.Refusal as exc:
        raise Refusal("autopilot state: " + str(exc)) from exc
    # A fabricated in-progress status must not bypass the between-task breaker.
    # Resumes are permitted only after an audited start for this attempt.
    text, fields = status(task)
    entries = log_entries(task) if entries is None else entries
    identity = resolved_attempt(text, fields, task, entries, pending_base=pending_base)
    started = any(
        e["allowed"]
        and e["attempt"] == identity
        and (
            e["action"] == "baseline"
            or e["action"] == "transition"
            and e["after"]["phase"] == "baseline"
        )
        for e in entries
    )
    if not started:
        for row in run["tasks"]:
            if row["id"] == task.name:
                row["phase"] = "approved"
    decision = autopilot.next_step(run)
    if decision != f"run {task.name}":
        raise Refusal("autopilot: " + decision)
    return matches[0].name


def authorize(entries: list[dict], event: dict, loaded: dict, task: Path) -> None:
    if not status(task)[1]["plan-approved"].startswith("yes ("):
        raise Refusal("status does not carry current plan approval")
    matches = [
        e
        for e in entries
        if e["allowed"]
        and e["action"] == "approve"
        and e["attempt"] == event["attempt"]
        and e["plan"] == event["plan"]
    ]
    if not matches:
        raise Refusal("missing current plan authorization; use approve")
    approval = matches[-1]
    event["source"] = approval["source"]
    if (
        approval["source"] != "user"
        and not policy.decide(
            "plan-approval", loaded, task.parent.parent, task / "plan.md"
        )["allowed"]
    ):
        raise Refusal("policy no longer authorizes this plan")


def local_branch(task: Path, branch: str) -> bool:
    """Check exact local refs, never revision expressions or shell command text."""
    ref = "refs/heads/" + branch
    for args in (["check-ref-format", ref], ["show-ref", "--verify", "--quiet", ref]):
        result = subprocess.run(
            ["git", *args], cwd=task.parent.parent, capture_output=True, check=False
        )
        if result.returncode:
            if args[0] == "show-ref" and result.returncode == 1:
                return False
            raise Refusal("cannot verify local branch")
    return True


def rebase_history(event: dict) -> str:
    link = event["rebase"]
    return f"- {event['time']} rebase — {link['old_base']} → {link['new_base']}; phase gate"


def rebase_status(
    task: Path,
    text: str,
    state: dict,
    entries: list[dict],
    event: dict,
    base: str | None,
) -> str | None:
    """Validate dispatch-time base selection before recording or publishing metadata."""
    import autopilot

    if state["phase"] != "approved" or state["round"] != 0:
        raise Refusal("rebase requires approved round 0")
    if state["worktree"] != "none" or (task / "worktree").exists():
        raise Refusal("rebase requires no task isolation")
    if any(
        e["allowed"]
        and e["attempt"] == event["attempt"]
        and (
            e["action"] == "baseline"
            or e["action"] == "transition"
            and e["after"] == {"phase": "baseline", "round": 0}
        )
        for e in entries
    ):
        raise Refusal("baseline has already started")
    if local_branch(task, state["branch"]):
        raise Refusal("task branch already exists")
    run = run_context(task, entries, pending_base=base)
    if run is None:
        raise Refusal("rebase requires one eligible linked run")
    event["run"] = run
    try:
        required = autopilot.base_line(str(task.parent / "_autopilot" / run), task.name)
    except autopilot.Refusal as exc:
        raise Refusal("autopilot base: " + str(exc)) from exc
    if base != required:
        raise Refusal("requested base differs from required stacked base")
    if not local_branch(task, required):
        raise Refusal("required local base branch does not exist")
    changes = [
        e
        for e in entries
        if e.get("rebase")
        and e["plan"] == event["plan"]
        and e["attempt"] == event["attempt"]
    ]
    if changes and changes[-1]["run"] != run:
        raise Refusal("linked run changed after controlled base selection")
    if base == state["base"]:
        return None
    raw = attempt(text, state, task)
    pending = (
        changes[-1] if changes and changes[-1]["rebase"]["old_attempt"] == raw else None
    )
    if pending is None:
        new_state = {**state, "base": base}
        event["rebase"] = {
            "schema": "base-change/1",
            "old_base": state["base"],
            "new_base": base,
            "old_attempt": raw,
            "new_attempt": attempt(text, new_state, task),
            "branch": state["branch"],
            "retries": attempt_inputs(text, state, task)[2],
        }
    else:
        if pending["rebase"]["new_base"] != base:
            raise Refusal("only the exact pending base change may resume")
    result = text.replace(f"- base: {state['base']}\n", f"- base: {base}\n", 1)
    return result.replace(
        "\n## Log\n",
        f"\n{rebase_history(pending or event)}\n## Log\n",
        1,
    )


def authorization_request(
    task: Path,
    action: str,
    ceiling: int,
    *,
    session: str,
    home: Path | None = None,
    signed: bool = False,
) -> dict | None:
    """Issue a prompt witness or prepare an unsigned request for the operator."""
    task = task.resolve()
    if task.parent.name != ".work" or not task.is_dir():
        raise Refusal("task must be an existing ordinary .work directory")
    loaded = policy.effective(policy.load(task.parent.parent, home or Path.home()))
    if loaded["mode"] == "legacy":
        if signed:
            raise Refusal("signed approval requires configured policy")
        return
    with locked(task):
        text, state = status(task)
        plan = read(task / "plan.md")
        validated = role_contracts.validate_plan(plan)
        if not validated.ok or validated.blocked:
            raise Refusal("invalid or blocked plan")
        entries = log_entries(task)
        token = digest(plan.encode("utf-8"))
        identity = resolved_attempt(text, state, task, entries)
        limit = max(
            [
                loaded["values"]["max_rounds"],
                *[
                    e["ceiling"]
                    for e in entries
                    if e["allowed"]
                    and e["action"] == "grant"
                    and e["source"] == "user"
                    and e["attempt"] == identity
                    and e["plan"] == token
                ],
            ]
        )
        if action == "approve":
            if (
                state["phase"] not in ("planned", "approved")
                or state["round"] != 0
                or ceiling != 0
            ):
                raise Refusal("approval requires planned round 0")
        elif action != "grant" or type(ceiling) is not int or ceiling <= limit:
            raise Refusal("grant needs a finite ceiling above the current limit")
        if signed:
            return signed_approval.request(
                signed_approval.scope(task, action, token, identity, ceiling)
            )
        approval_witness.issue(
            task,
            action,
            token,
            identity,
            ceiling,
            channel="user-prompt",
            session=session,
        )


def decide(
    task: Path,
    action: str,
    *,
    number: int | None = None,
    target: str | None = None,
    source: str = "policy",
    ceiling: int = 0,
    home: Path | None = None,
    worktree: str | None = None,
    message: str | None = None,
    base: str | None = None,
    signed_proof: tuple[Path, Path] | None = None,
) -> dict:
    """Record before returning permission; legacy repositories retain existing behaviour."""
    task = task.resolve()
    if task.parent.name != ".work" or not task.is_dir():
        raise Refusal("task must be an existing ordinary .work directory")
    loaded = policy.effective(policy.load(task.parent.parent, home or Path.home()))
    if loaded["mode"] == "legacy":
        if signed_proof is not None:
            raise Refusal("signed approval requires configured policy")
        return {"allowed": True, "reason": "legacy"}
    try:
        with locked(task):
            entries = log_entries(task)
            event = {
                "schema": SCHEMA,
                "task": task.name,
                "run": None,
                "time": datetime.now(UTC).isoformat(),
                "action": action,
                "before": None,
                "after": None,
                "allowed": False,
                "reason": "refused",
                "policy": digest(json.dumps(loaded, sort_keys=True).encode()),
                "source": source
                if source in ("user", "policy", "--auto")
                else "policy",
                "plan": digest(b""),
                "attempt": digest(b""),
                "ceiling": ceiling if type(ceiling) is int and ceiling >= 0 else 0,
            }
            result_text = None
            try:
                text, state = status(task)
                before = {k: state[k] for k in ("phase", "round")}
                event["before"] = event["after"] = before
                event["attempt"] = resolved_attempt(
                    text,
                    state,
                    task,
                    entries,
                    pending_base=base if action == "rebase" else None,
                )
                plan = read(task / "plan.md")
                event["plan"] = digest(plan.encode("utf-8"))
                validated = role_contracts.validate_plan(plan)
                if not validated.ok or validated.blocked:
                    raise Refusal("invalid or blocked plan")
                commands = validated.commands
                if source not in ("user", "policy", "--auto"):
                    raise Refusal("invalid authorization source")
                limit = loaded["values"]["max_rounds"]
                for entry in entries:
                    if (
                        entry["action"] == "grant"
                        and entry["allowed"]
                        and entry["source"] == "user"
                        and entry["attempt"] == event["attempt"]
                        and entry["plan"] == event["plan"]
                    ):
                        limit = max(limit, entry["ceiling"])
                if action in ("approve", "grant"):
                    if action == "grant":
                        if (
                            source != "user"
                            or type(ceiling) is not int
                            or ceiling <= limit
                        ):
                            raise Refusal(
                                "grant needs a human and finite ceiling above the current limit"
                            )
                    elif (
                        source != "user"
                        and not policy.decide(
                            "plan-approval",
                            loaded,
                            task.parent.parent,
                            task / "plan.md",
                        )["allowed"]
                    ):
                        raise Refusal("approval gate: policy requires human approval")
                    if action == "approve":
                        if (
                            state["phase"] not in ("planned", "approved")
                            or state["round"] != 0
                        ):
                            raise Refusal("approval requires planned round 0")
                        event["after"] = {"phase": "approved", "round": 0}
                        result_text = replace_status(text, state, "approved", 0, source)
                    if source == "user":
                        if signed_proof is not None:
                            binding = signed_approval.scope(
                                task, action, event["plan"], event["attempt"], ceiling
                            )
                            event["authorization"] = signed_approval.consume(
                                binding, task, *signed_proof
                            )
                        else:
                            event["authorization"] = approval_witness.consume(
                                task, action, event["plan"], event["attempt"], ceiling
                            )
                elif action == "rebase":
                    authorize(entries, event, loaded, task)
                    event["ceiling"] = 0
                    result_text = rebase_status(task, text, state, entries, event, base)
                elif action == "transition":
                    if target not in PHASES:
                        raise Refusal("unknown target phase")
                    new_round = state["round"] if number is None else number
                    if target != "implementing" and new_round != state["round"]:
                        raise Refusal("round changes only when implementation starts")
                    if target == state["phase"] and new_round == state["round"]:
                        # A repeated request still appends a decision, then revalidates protected evidence.
                        pass
                    elif target not in EDGES[state["phase"]]:
                        raise Refusal("illegal phase jump")
                    if target == "approved":
                        raise Refusal("use approve for the approval transition")
                    if target == "implementing":
                        expected = state["round"] + 1
                        if state["phase"] == "implementing":
                            expected = state["round"]
                        if new_round != expected:
                            raise Refusal(
                                "implementation must use the next round or resume the current round"
                            )
                        authorize(entries, event, loaded, task)
                        checks(task, 0, commands)
                        if new_round > limit:
                            raise Refusal("round cap")
                        if new_round > 1:
                            reason = stopped(task, new_round - 1, plan, commands, limit)
                            if reason:
                                raise Refusal(reason)
                        event["run"] = run_context(task, entries)
                    elif target in (
                        "baseline",
                        "checking",
                        "reviewing",
                        "review-approved",
                        "done",
                    ):
                        authorize(entries, event, loaded, task)
                        if new_round != state["round"]:
                            raise Refusal(
                                "round changes only when implementation starts"
                            )
                        if target == "baseline" and new_round != 0:
                            raise Refusal("baseline requires round 0")
                        if target in (
                            "checking",
                            "reviewing",
                            "review-approved",
                            "done",
                        ):
                            checks(task, 0, commands)
                        if target == "checking" and loaded["publish"]["enabled"]:
                            import publish_task

                            publish_task.capture_check(task, home=home or Path.home())
                        if target == "reviewing":
                            checks(task, new_round, commands)
                            if loaded["publish"]["enabled"]:
                                import publish_task

                                publish_task.capture_review(
                                    task, home=home or Path.home()
                                )
                        if (
                            target in ("review-approved", "done")
                            and review(task, new_round, plan, commands)[0] != "APPROVED"
                        ):
                            raise Refusal("finish requires APPROVED review")
                        if target == "review-approved" and loaded["publish"]["enabled"]:
                            import publish_task

                            publish_task.seal_review(task, home=home or Path.home())
                        if target == "baseline":
                            event["run"] = run_context(task, entries)
                    elif target == "escalated":
                        if new_round != state["round"] or not stopped(
                            task, new_round, plan, commands, limit
                        ) in ("round cap", "no progress"):
                            raise Refusal("escalation requires a mandatory stop")
                    elif target == "blocked":
                        if not re.search(
                            r"^BLOCKED: \S", text.split("\n## Log\n")[1], re.MULTILINE
                        ):
                            raise Refusal(
                                "blocked requires implementer evidence of a wrong plan"
                            )
                    elif new_round != state["round"]:
                        raise Refusal("round changes only when implementation starts")
                    event["after"] = {"phase": target, "round": new_round}
                    result_text = replace_status(text, state, target, new_round)
                    if worktree is not None:
                        if (
                            state["round"] != 0
                            or target != "baseline"
                            or worktree not in ("none", f".work/{task.name}/worktree")
                        ):
                            raise Refusal(
                                "worktree metadata can only be set at baseline round 0"
                            )
                        result_text = result_text.replace(
                            f"- worktree: {state['worktree']}\n",
                            f"- worktree: {worktree}\n",
                            1,
                        )
                elif action == "note":
                    authorize(entries, event, loaded, task)
                    if not message or "\n" in message or "\r" in message:
                        raise Refusal("note requires one non-empty message line")
                    result_text = (
                        text + ("" if text.endswith("\n") else "\n") + message + "\n"
                    )
                elif action == "dispatch":
                    authorize(entries, event, loaded, task)
                    event["run"] = run_context(task, entries)
                elif action == "direct-status-mutation":
                    raise Refusal(
                        "use phase_gate.py transition; direct status mutation is refused"
                    )
                elif action in (
                    "implement",
                    "prepare",
                    "baseline",
                    "check",
                    "finish",
                    "prune",
                ):
                    authorize(entries, event, loaded, task)
                    requested = state["round"] if number is None else number
                    if action == "prepare":
                        if (
                            state["phase"] not in ("approved", "baseline")
                            or state["round"] != 0
                            or requested != 1
                        ):
                            raise Refusal(
                                "routing preparation requires approved baseline round 0"
                            )
                        checks(task, 0, commands)
                    else:
                        expected_phase = {
                            "implement": "implementing",
                            "baseline": "baseline",
                            "check": "checking",
                            "finish": "review-approved",
                            "prune": "done",
                        }[action]
                        if (
                            state["phase"] != expected_phase
                            or requested != state["round"]
                        ):
                            raise Refusal(
                                "operation does not match current phase and round"
                            )
                        if action == "baseline":
                            if requested != 0:
                                raise Refusal("baseline requires round 0")
                        else:
                            if requested < 1 or requested > limit:
                                raise Refusal("round cap or invalid round")
                            checks(task, 0, commands)
                        if action == "check" and loaded["publish"]["enabled"]:
                            import publish_task

                            publish_task.capture_check(task, home=home or Path.home())
                        if action == "implement" and requested > 1:
                            reason = stopped(task, requested - 1, plan, commands, limit)
                            if reason:
                                raise Refusal(reason)
                        if (
                            action in ("finish", "prune")
                            and review(task, requested, plan, commands)[0] != "APPROVED"
                        ):
                            raise Refusal("finish requires APPROVED review")
                    event["run"] = (
                        run_context(task, entries)
                        if action not in ("finish", "prune")
                        else None
                    )
                else:
                    raise Refusal("unsupported gate action")
                event.update(allowed=True, reason="allowed")
            except (ValueError, OSError) as exc:
                event.pop("rebase", None)
                event["reason"] = str(exc)
                event["after"] = event["before"]
            append(task, event)
            if not event["allowed"]:
                raise Refusal(event["reason"])
            if result_text is not None:
                publish(task / "status.md", result_text)
            return event
    except (OSError, policy.ConfigError) as exc:
        raise Refusal("gate evidence unavailable; protected operation refused") from exc


def replace_status(
    text: str, fields: dict, phase: str, number: int, source: str | None = None
) -> str:
    before = text
    text = text.replace(f"- phase: {fields['phase']}\n", f"- phase: {phase}\n", 1)
    text = text.replace(f"- round: {fields['round']}\n", f"- round: {number}\n", 1)
    if source:
        text = text.replace(
            f"- plan-approved: {fields['plan-approved']}\n",
            f"- plan-approved: yes ({source}, {datetime.now(UTC).date()})\n",
            1,
        )
    if phase == "review-approved":
        text = text.replace(
            f"- last-verdict: {fields['last-verdict']}\n",
            "- last-verdict: APPROVED\n",
            1,
        )
    elif fields["phase"] in ("reviewing", "escalated") and phase != fields["phase"]:
        text = text.replace(
            f"- last-verdict: {fields['last-verdict']}\n",
            "- last-verdict: CHANGES_REQUESTED\n",
            1,
        )
    if text != before:
        stamp = datetime.now(UTC).isoformat()
        text = text.replace(
            "\n## Log\n",
            f"\n- {stamp} {phase} — round {number}; phase gate\n## Log\n",
            1,
        )
    return text


def publish(
    path: Path,
    text: str,
    *,
    replace: Callable[[str, Path], None] = os.replace,
    pause: Callable[[float], None] = time.sleep,
) -> None:
    fd, name = tempfile.mkstemp(prefix=".gate-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for retry in range(5):
            try:
                replace(name, path)
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (32, 33) or retry == 4:
                    raise
                pause(0.01 * 2**retry)
    finally:
        Path(name).unlink(missing_ok=True)


def human_confirmation(task: Path, action: str, ceiling: int) -> tuple[str, str]:
    """Only called by the CLI; hooks never obtain authority from model command text."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Refusal("run human authorization in a separate interactive terminal")
    token = digest((task / "plan.md").read_bytes())
    text, fields = status(task)
    attempt_token = resolved_attempt(text, fields, task, log_entries(task))
    challenge = f"{action} {task.name} {token} {attempt_token} {ceiling}"
    print(f"Review {task / 'plan.md'}; type exactly: {challenge}")
    if input().strip() != challenge:
        raise Refusal("human authorization was not confirmed")
    return token, attempt_token


def project_from_cwd(cwd: Path) -> Path:
    for parent in (cwd, *cwd.parents):
        if parent.name == "worktree" and parent.parent.parent.name == ".work":
            return parent.parent.parent.parent
    for parent in (cwd, *cwd.parents):
        if parent.name == ".work":
            return parent.parent
        if (parent / ".git").exists():
            return parent
    return cwd


def hook(payload: dict, *, home: Path | None = None) -> str | None:
    """Recognize exact real user commands; other prompts only select context."""
    if not isinstance(payload, dict):
        return "workflow hook payload must be an object"
    if not isinstance(payload.get("tool_input", {}), dict):
        return "workflow hook tool_input must be an object"
    cwd = Path(str(payload.get("cwd") or Path.cwd())).resolve()
    project = project_from_cwd(cwd)
    event = payload.get("hook_event_name", "PreToolUse")
    tool = payload.get("tool_name")
    inputs = payload.get("tool_input") or {}
    prompt = str(inputs.get("prompt") or "")
    command = str(inputs.get("command") or "")
    if event != "UserPromptSubmit":
        denial = None
        if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            denial = witness_write(
                str(inputs.get("file_path") or inputs.get("notebook_path") or ""), cwd
            )
        elif tool in ("Bash", "PowerShell"):
            denial = witness_command(command, cwd)
        if denial:
            return "workflow gate: " + denial
    launch_role = inputs.get("subagent_type")
    launch = tool in ("Agent", "Task") and launch_role in (
        "task-flow:implementer",
        "task-flow:checker",
    )
    skill = tool == "Skill" and inputs.get("skill") == "task-flow:task-finish"
    path = str(inputs.get("file_path") or "").replace("\\", "/")
    mutation = tool in ("Edit", "Write", "MultiEdit") and bool(
        re.search(r"(?:^|/)\.work/[^/]+/status\.md$", path)
    )
    shell = tool in ("Bash", "PowerShell")
    status_mutation = (
        shell
        and "status.md" in command
        and bool(
            re.search(
                r"\b(Set-Content|Add-Content|Out-File|sed|perl|python|python3|tee)\b|>",
                command,
                re.IGNORECASE,
            )
        )
    )
    # Match a program argument, not filenames in tests, read commands or Git history.
    script_path = r"[^\s\"';&|\r\n]*[/\\]scripts[/\\](?:implement|gates|task_record|prune_task)\.py"
    quoted_script_path = script_path.replace(r"\s", "")
    script = (
        re.search(
            r"(?:^|[;&|\r\n])\s*(?:&\s+)?"
            r"(?:(?:python(?:3(?:\.\d+)?)?|py)(?:\.exe)?\s+|uv(?:\.exe)?\s+run\s+--script\s+)?"
            rf"(?P<program>\"{quoted_script_path}\"|'{quoted_script_path}'|{script_path})(?=\s|$)"
            r"(?P<arguments>[^;&|\r\n]*)",
            command,
        )
        if shell
        else None
    )
    phase_cli = shell and "phase_gate.py" in command
    finish = shell and bool(
        re.search(r"\bgit\s+(?:-[^\s]+\s+[^\s]+\s+)*commit\b", command)
    )
    if event != "UserPromptSubmit" and not any(
        (launch, skill, mutation, status_mutation, script, phase_cli, finish)
    ):
        return None
    try:
        session = str(payload.get("session_id") or "")
        context_file = (
            project / ".work" / (".gate-context-" + digest(session.encode()) + ".json")
        )
        match = re.fullmatch(
            r"/task-flow:task-(?:run|check|finish) ([a-z0-9][a-z0-9-]*)",
            str(payload.get("prompt") or "").strip(),
        )
        authorization = re.fullmatch(
            r"/task-flow:task-(approve|grant) ([a-z0-9][a-z0-9-]*)(?: ([1-9][0-9]*))?",
            str(payload.get("prompt") or ""),
        )
        if event == "UserPromptSubmit" and authorization:
            action, task_id, raw_ceiling = authorization.groups()
            if (
                (action == "grant") != (raw_ceiling is not None)
                or not isinstance(payload.get("session_id"), str)
                or not session.strip()
                or payload.get("agent_type")
                or tool
                or inputs
            ):
                return "workflow gate: authorization requires an exact real user command and session"
            authorization_request(
                project / ".work" / task_id,
                action,
                int(raw_ceiling) if raw_ceiling else 0,
                session=session,
                home=home,
            )
            return None
        if event == "UserPromptSubmit" and not match:
            if session:
                context_file.unlink(missing_ok=True)
            return None
        loaded = policy.load(project, home or Path.home())
        if loaded["mode"] == "legacy":
            return None
        policy.effective(loaded)
        if event == "UserPromptSubmit":
            # Context is not authority: even an automatically submitted command can only
            # select a task whose approval is independently verified by the gate.
            if match and session and (project / ".work" / match[1]).is_dir():
                publish(context_file, json.dumps({"task": match[1]}) + "\n")
            elif session:
                context_file.unlink(missing_ok=True)
            return None
        if phase_cli:
            if re.search(r"\brecover\b", command) and re.search(
                r"--source(?:=|\s+)[\"']?user\b", command
            ):
                raise Refusal(
                    "audit recovery must run in a separate terminal, outside model tools"
                )
            if not mutation and not status_mutation:
                return None  # The CLI itself checks and records each transition.
        candidates = set()
        for value in re.findall(r"(?im)^Task directory:\s*(.+?)\s*$", prompt):
            candidates.add(Path(value.strip().strip('"')).resolve())
        for value in re.findall(
            r"--task-dir(?:=|\s+)(\"[^\"]+\"|'[^']+'|[^\s;]+)", command
        ):
            candidate = Path(value.strip("\"'"))
            candidates.add(
                (cwd / candidate).resolve()
                if not candidate.is_absolute()
                else candidate.resolve()
            )
        if mutation:
            candidate = Path(path)
            candidates.add(
                ((cwd / candidate) if not candidate.is_absolute() else candidate)
                .resolve()
                .parent
            )
        if skill:
            argument = str(inputs.get("args") or "").strip()
            if re.fullmatch(r"[a-z0-9][a-z0-9-]*", argument):
                candidates.add(project / ".work" / argument)
        if session and context_file.exists():
            context = json.loads(read(context_file))
            candidates.add(project / ".work" / context["task"])
        if (
            (script or finish)
            and not (launch or skill or mutation or status_mutation)
            and not candidates
            and not str(payload.get("agent_type") or "").startswith("task-flow:")
        ):
            return None
        if len(candidates) != 1:
            raise Refusal("missing or ambiguous explicit task context")
        task = candidates.pop()
        if task.parent != project / ".work":
            raise Refusal("task context is outside the workflow repository")
        if mutation or status_mutation:
            # Record the scoped refusal; transitions own status, preserving all other text.
            try:
                decide(task, "direct-status-mutation", home=home)
            except Refusal:
                pass
            raise Refusal(
                "use phase_gate.py transition; direct status mutation is refused"
            )
        if script:
            if script["program"].strip("\"'").endswith("task_record.py") and re.match(
                r"\s+(?:render|trailers)\b", script["arguments"]
            ):
                decide(task, "finish", home=home)
            # Implementation, checks and pruning also enforce their own preconditions.
            return None
        action = "implement" if launch else "finish"
        if launch and launch_role == "task-flow:checker":
            action = "baseline" if status(task)[1]["round"] == 0 else "check"
        decide(task, action, home=home)
        return None
    except (Refusal, ValueError, OSError, KeyError, TypeError) as exc:
        return f"workflow gate: {exc}"


def hook_main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        reason = hook(payload)
    except (ValueError, TypeError, OSError):
        reason = "workflow hook payload unavailable"
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "action",
        choices=(
            "initialize",
            "approval-request",
            "rebase",
            "transition",
            "approve",
            "grant",
            "recover",
            "note",
            "implement",
            "prepare",
            "baseline",
            "check",
            "finish",
        ),
    )
    parser.add_argument("--task-dir", required=True, type=Path)
    parser.add_argument("--round", type=int)
    parser.add_argument("--to", choices=sorted(PHASES))
    parser.add_argument(
        "--source", choices=("user", "policy", "--auto"), default="policy"
    )
    parser.add_argument("--ceiling", type=int, default=0)
    parser.add_argument("--base")
    parser.add_argument("--worktree")
    parser.add_argument("--message")
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--signature", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        proof = None
        if args.request_file is not None or args.signature is not None:
            if (
                args.request_file is None
                or args.signature is None
                or args.action not in ("approve", "grant")
                or args.source != "user"
            ):
                raise Refusal(
                    "signed approval requires approve or grant, --source user, and both proof files"
                )
            proof = (args.request_file, args.signature)
        if args.action == "approval-request":
            result = authorization_request(
                args.task_dir,
                "grant" if args.ceiling else "approve",
                args.ceiling,
                session="",
                signed=True,
            )
            text = json.dumps(result, sort_keys=True) + "\n"
            if args.output is None:
                print(text, end="")
            else:
                with args.output.open("xb") as handle:
                    handle.write(text.encode("utf-8"))
                print(f"wrote approval request to {args.output}")
            return 0
        if args.output is not None:
            raise Refusal("--output is only valid for approval-request")
        if args.action == "initialize":
            initialize(args.task_dir, args.base or "")
            print("initialized unapproved task")
            return 0
        human = args.source == "user" and args.action == "recover"
        if human:
            human_confirmation(args.task_dir, args.action, args.ceiling)
        if args.action == "recover":
            recover(args.task_dir, human=human)
            print("recovered interrupted tail; retry the original operation")
            return 0
        result = decide(
            args.task_dir,
            args.action,
            number=args.round,
            target=args.to,
            source=args.source,
            ceiling=args.ceiling,
            worktree=args.worktree,
            message=args.message,
            base=args.base,
            signed_proof=proof,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (Refusal, policy.ConfigError, OSError) as exc:
        print(f"phase gate refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(hook_main() if sys.argv[1:] == ["hook"] else main())
