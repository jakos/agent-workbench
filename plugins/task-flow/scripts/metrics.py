# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Offline delivery evidence: read-only report and explicit atomic publication."""

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# The CLI's read-only contract includes Python's import cache.
sys.dont_write_bytecode = True
import policy

SCHEMA = "delivery-metrics/1"
START = "<!-- metrics:baseline:start -->"
END = "<!-- metrics:baseline:end -->"
MAX_BYTES = 4_000_000


class EvidenceError(ValueError):
    """Invalid measurement evidence; publication must leave the output unchanged."""


@dataclass(frozen=True)
class Change:
    id: str
    timestamp: str
    provenance: str
    cohort: str = "unknown"


@dataclass(frozen=True)
class Deploy:
    id: str
    timestamp: str
    provenance: str
    commit: str
    changes: tuple[str, ...]


@dataclass(frozen=True)
class Failure:
    id: str
    timestamp: str
    provenance: str
    deployment: str
    rework: tuple[str, ...]


@dataclass(frozen=True)
class Recovery:
    id: str
    timestamp: str
    provenance: str
    failure: str


def timestamp(value: str | datetime) -> datetime:
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.utcoffset() is None:
            raise ValueError("timestamp requires timezone")
        return result.astimezone(UTC)
    except (TypeError, ValueError, AttributeError):
        raise ValueError("invalid timezone-aware timestamp") from None


def iso(value: str | datetime) -> str:
    return timestamp(value).isoformat().replace("+00:00", "Z")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )
    if result.returncode:
        raise ValueError("git evidence could not be read")
    return result.stdout


def history(repo: Path, revision: str) -> tuple[str, list[tuple[str, str, str]]]:
    revision = git(
        repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"
    ).strip()
    fields = git(
        repo,
        "log",
        "--reverse",
        "--topo-order",
        "--format=%H%x00%cI%x00%B%x00",
        revision,
    ).split("\0")
    commits = [
        (fields[i].strip(), iso(fields[i + 1]), fields[i + 2])
        for i in range(0, len(fields) - 1, 3)
    ]
    return revision, commits


def git_events(
    repo: Path, revision: str, prefix: str, cutoff: datetime
) -> tuple[str, list[Change], list[Deploy], list[tuple[str, str, str]]]:
    revision, commits = history(repo, revision)
    reachable = {c[0] for c in commits}
    candidates = []
    for tag in git(repo, "tag", "--list").splitlines():
        if not tag.startswith(prefix):
            continue
        commit = git(repo, "rev-parse", "--verify", "refs/tags/" + tag + "^{}").strip()
        if commit not in reachable:
            continue
        date = next(c[1] for c in commits if c[0] == commit)
        if timestamp(date) < cutoff:
            candidates.append((date, tag, commit))
    changes = []
    for commit, date, body in commits:
        implementers = re.findall(
            r"^Task-Implementer: (.+)$", trailers(body), re.MULTILINE
        )
        cohort = cohort_for(implementers)
        changes.append(Change(commit, date, "git:" + commit, cohort))
    assigned = set()
    deploys = []
    for date, tag, commit in sorted(candidates):
        ancestors = set(git(repo, "rev-list", commit).splitlines())
        selected = tuple(sorted(ancestors - assigned))
        assigned.update(ancestors)
        deploys.append(Deploy(tag, date, "git:refs/tags/" + tag, commit, selected))
    return revision, changes, deploys, commits


def incident_events(
    path: Path | None, deploys: list[Deploy]
) -> tuple[list[Failure], list[Recovery]]:
    if path is None:
        return [], []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if (
        set(data) - {"version", "incidents"}
        or type(data.get("version")) is not int
        or data["version"] != 1
    ):
        raise ValueError("incidents: expected version = 1 and incidents only")
    rows = data.get("incidents", [])
    if not isinstance(rows, list):
        raise EvidenceError("incidents: expected array of tables")
    tags = {d.id: timestamp(d.timestamp) for d in deploys}
    failures, recoveries, identities = [], [], set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) - {"id", "deployment", "failed_at", "recovered_at", "rework"}
            or not {"id", "deployment", "failed_at"} <= set(row)
        ):
            raise ValueError("incident: invalid keys")
        identity = row["id"]
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", identity)
            or identity in identities
        ):
            raise ValueError("incident: invalid or duplicate id")
        identities.add(identity)
        failed = timestamp(row["failed_at"])
        deployment = row["deployment"]
        rework = row.get("rework", [])
        if (
            not isinstance(deployment, str)
            or deployment not in tags
            or not isinstance(rework, list)
            or any(not isinstance(t, str) or t not in tags for t in rework)
            or len(set(rework)) != len(rework)
        ):
            raise ValueError("incident: invalid deployment reference")
        if failed < tags[deployment] or any(tags[t] < failed for t in rework):
            raise ValueError("incident: deployment precedes failure")
        source = "incidents:" + identity
        failures.append(
            Failure(identity, iso(failed), source, deployment, tuple(sorted(rework)))
        )
        if "recovered_at" in row:
            recovered = timestamp(row["recovered_at"])
            if recovered < failed:
                raise ValueError("incident: recovery precedes failure")
            recoveries.append(
                Recovery(identity + ":recovery", iso(recovered), source, identity)
            )
    return sorted(failures, key=lambda f: f.id), sorted(recoveries, key=lambda r: r.id)


def cohort_for(implementers: list[str]) -> str:
    known = [p for p in implementers if p not in ("unknown/unknown", "unknown")]
    if any(p != "human/human" for p in known):
        return "agent"
    return "non-agent" if known else "unknown"


def trailers(body: str) -> str:
    """Only the final trailer paragraph is attribution evidence, never quoted prose."""
    paragraph = body.strip().split("\n\n")[-1]
    if all(
        re.match(r"^[A-Za-z][A-Za-z0-9-]*: .+$", line)
        for line in paragraph.splitlines()
    ):
        return paragraph
    return ""


def summary_escalation(directory: Path, final: str | None) -> bool | None:
    path = directory / "summary.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    section = re.search(
        r"^## Rounds\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if section is None:
        return None
    if re.search(r"\b(?:round cap|no progress|escalated)\b", section[1], re.IGNORECASE):
        return True
    return False if final == "APPROVED" else None


def merge_task(tasks: dict, identity: str, incoming: dict) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", identity):
        raise ValueError("invalid task identity")
    current = tasks.setdefault(identity, {"id": identity, "sources": [], "calls": {}})
    for key, value in incoming.items():
        if key == "sources":
            current[key] = sorted(set(current[key] + value))
        elif key in ("calls", "reviews"):
            current.setdefault(key, {})
            for invocation, call in value.items():
                if invocation in current[key] and current[key][invocation] != call:
                    raise ValueError("contradictory invocation evidence")
                current[key][invocation] = call
        elif value is not None:
            if key in current and current[key] != value:
                raise ValueError("contradictory task evidence: " + key)
            current[key] = value


def trailer_tasks(
    commits: list[tuple[str, str, str]], tasks: dict, dates: dict
) -> None:
    for commit, date, body in commits:
        body = trailers(body)
        ids = re.findall(r"^Task-Id: (.+)$", body, re.MULTILINE)
        if not ids:
            continue
        if len(ids) != 1:
            raise ValueError("duplicate task trailer identity")
        identity = ids[0]
        dates[identity] = min(date, dates.get(identity, date))
        rounds = re.findall(r"^Task-Rounds: (\d+)$", body, re.MULTILINE)
        incoming = {"sources": ["git:" + commit]}
        if rounds:
            if len(rounds) != 1 or int(rounds[0]) < 1:
                raise ValueError("invalid task rounds")
            incoming["rounds"] = int(rounds[0])
        implementers = re.findall(
            r"^Task-Implementer: (.+)$", trailers(body), re.MULTILINE
        )
        cohort = cohort_for(implementers)
        if cohort != "unknown":
            incoming["cohort"] = cohort
        merge_task(tasks, identity, incoming)


def record_tasks(repo: Path, revision: str, tasks: dict, dates: dict) -> None:
    paths = git(
        repo, "ls-tree", "-r", "--name-only", revision, "--", "docs/tasks/"
    ).splitlines()
    for path in paths:
        if not path.endswith(".md"):
            continue
        text = git(repo, "show", revision + ":" + path)
        if "- schema: task-record/1" not in text:
            continue
        identity = Path(path).stem
        record_id = re.findall(r"^- task: (.+)$", text, re.MULTILINE)
        if record_id != [identity]:
            raise ValueError("task record identity mismatch")
        first = git(
            repo, "log", "--reverse", "--format=%cI", revision, "--", path
        ).splitlines()[0]
        date = iso(first)
        dates[identity] = min(date, dates.get(identity, date))
        incoming = {"sources": ["git:" + revision + ":" + path]}
        for field, pattern in (
            ("rounds", r"^- rounds: (\d+)$"),
            ("final", r"^- final-verdict: (.+)$"),
        ):
            matches = re.findall(pattern, text, re.MULTILINE)
            if len(matches) != 1:
                raise ValueError("invalid task record")
            incoming[field] = int(matches[0]) if field == "rounds" else matches[0]
        reviews = re.findall(
            r"^- round (\d+): (APPROVED|CHANGES_REQUESTED)", text, re.MULTILINE
        )
        if (
            incoming["rounds"] < 1
            or len(reviews) != incoming["rounds"]
            or {int(n) for n, _ in reviews} != set(range(1, incoming["rounds"] + 1))
            or dict(reviews).get(str(incoming["rounds"])) != incoming["final"]
        ):
            raise ValueError("contradictory task record rounds or verdict")
        incoming["reviews"] = dict(reviews)
        implementers = re.findall(
            r"^- round \d+: (?:APPROVED|CHANGES_REQUESTED) — implementer (\S+)$",
            text,
            re.MULTILINE,
        )
        known = cohort_for(implementers)
        if known != "unknown":
            incoming["cohort"] = known
        merge_task(tasks, identity, incoming)


def local_reviews(directory: Path) -> dict:
    reviews = {}
    for review in sorted(directory.glob("review-*.md")):
        match = re.fullmatch(r"review-(\d+)\.md", review.name)
        if not match:
            continue
        verdict = re.findall(
            r"^## Verdict: (APPROVED|CHANGES_REQUESTED)$",
            review.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if len(verdict) != 1:
            raise ValueError("invalid local review")
        reviews[match[1]] = verdict[0]
    return reviews


def local_calls(directory: Path) -> tuple[dict, bool]:
    calls = {}
    affirmative = False
    for metric in sorted(
        [*directory.glob("impl-*.json"), *directory.glob("role-*.json")]
    ):
        data = json.loads(metric.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise EvidenceError("invalid local metrics object")
        if metric.name.startswith("impl-"):
            if not re.fullmatch(r"impl-[1-9][0-9]*\.json", metric.name):
                raise ValueError("invalid implementer round identity")
            invocation, role = metric.stem, "implementer"
            affirmative |= data.get("kind") == "claude" or any(
                isinstance(data.get(key), str) and data[key] not in ("", "unknown")
                for key in ("profile", "model")
            )
        else:
            invocation, role = data.get("invocation_id"), data.get("role")
            if (
                not isinstance(invocation, str)
                or role not in ("planner", "reviewer", "backlog")
                or data.get("task_id") != directory.name
            ):
                raise ValueError("invalid producer invocation identity")
            invocation = role + ":" + invocation
            affirmative = True
        cost, known = data.get("cost_usd"), data.get("cost_known_usd")
        for value in (cost, known):
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
            ):
                raise ValueError("invalid recorded cost")
        if cost is not None and known is not None and known > cost:
            raise ValueError("known cost exceeds total")
        call = {
            "role": role,
            "cost": cost,
            "known": cost if cost is not None else known if known else None,
        }
        if invocation in calls and calls[invocation] != call:
            raise ValueError("contradictory invocation evidence")
        calls[invocation] = call
    return calls, affirmative


def local_task(directory: Path) -> dict:
    status = (directory / "status.md").read_text(encoding="utf-8")
    if re.findall(r"^# Status: (.+)$", status, re.MULTILINE) != [directory.name]:
        raise ValueError("local task identity mismatch")
    incoming = {"sources": ["local:" + directory.name], "calls": {}}
    rounds = re.findall(r"^- round: (\d+)$", status, re.MULTILINE)
    if len(rounds) != 1:
        raise ValueError("invalid local task round")
    incoming["rounds"] = int(rounds[0])
    reviews = local_reviews(directory)
    if reviews:
        incoming["reviews"] = reviews
        if str(incoming["rounds"]) in reviews:
            incoming["final"] = reviews[str(incoming["rounds"])]
    incoming["escalated"] = summary_escalation(directory, incoming.get("final"))
    incoming["calls"], affirmative = local_calls(directory)
    if affirmative:
        incoming["cohort"] = "agent"
    return incoming


def task_events(
    repo: Path, revision: str, commits: list[tuple[str, str, str]], root: Path | None
) -> list[dict]:
    tasks, dates = {}, {}
    trailer_tasks(commits, tasks, dates)
    record_tasks(repo, revision, tasks, dates)
    if root is not None:
        for directory in sorted(root.iterdir()):
            if directory.is_dir() and (directory / "status.md").is_file():
                merge_task(tasks, directory.name, local_task(directory))
    for identity, task in tasks.items():
        task["completed_at"] = dates.get(identity)
        task.setdefault("cohort", "unknown")
    return sorted(tasks.values(), key=lambda t: t["id"])


def ratio(numerator: float, denominator: float) -> dict:
    return {
        "value": numerator / denominator if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
    }


def median(values: list[float]) -> dict:
    return {
        "value": statistics.median(values) if values else None,
        "count": len(values),
    }


def agent_metrics(tasks: list[dict]) -> dict:
    reviewed = [t for t in tasks if "1" in t.get("reviews", {})]
    rounded = [t for t in tasks if t.get("rounds", 0) > 0]
    escalated = [t for t in tasks if t.get("escalated") is not None]
    calls = [c for t in tasks for c in t["calls"].values()]
    known = [c for c in calls if c["known"] is not None]
    cost_tasks = [
        t for t in tasks if any(c["known"] is not None for c in t["calls"].values())
    ]
    complete_tasks = [
        t for t in cost_tasks if all(c["cost"] is not None for c in t["calls"].values())
    ]
    total = sum(c["known"] for c in known)
    return {
        "tasks": len(tasks),
        "first_round_acceptance": ratio(
            sum(t["reviews"]["1"] == "APPROVED" for t in reviewed), len(reviewed)
        ),
        "rounds_per_task": ratio(sum(t["rounds"] for t in rounded), len(rounded)),
        "escalations": ratio(sum(t["escalated"] for t in escalated), len(escalated)),
        "known_cost_per_task_usd": ratio(total, len(cost_tasks)),
        "reviewer_cost_share": ratio(
            sum(c["known"] for c in known if c["role"] == "reviewer"), total
        ),
        "cost_coverage": {
            "calls": len(calls),
            "known_calls": len(known),
            "complete_calls": sum(c["cost"] is not None for c in calls),
            "tasks_with_cost": len(cost_tasks),
            "tasks_with_complete_recorded_calls": len(complete_tasks),
            "population_tasks": len(tasks),
            "label": "partial"
            if len(cost_tasks) < len(tasks)
            or len(known) != len(calls)
            or any(c["cost"] is None for c in calls)
            else "recorded calls only",
        },
        "plan_approval_without_edits": None,
        "supervision_burden": None,
        "escape_rate": None,
    }


def aggregate(snapshot: dict, start: datetime, end: datetime) -> dict:
    changes = {c["id"]: c for c in snapshot["changes"]}
    deploys = [
        d for d in snapshot["deploys"] if start <= timestamp(d["timestamp"]) < end
    ]
    ids = {d["id"] for d in deploys}
    durations, negative = [], 0
    cohorts = {c: 0 for c in ("agent", "non-agent", "unknown")}
    for deploy in deploys:
        for identity in deploy["changes"]:
            change = changes[identity]
            cohorts[change["cohort"]] += 1
            seconds = (
                timestamp(deploy["timestamp"]) - timestamp(change["timestamp"])
            ).total_seconds()
            if seconds < 0:
                negative += 1
            else:
                durations.append(seconds)
    failures = [f for f in snapshot["failures"] if timestamp(f["timestamp"]) < end]
    recovered = {
        r["failure"]: r
        for r in snapshot["recoveries"]
        if timestamp(r["timestamp"]) < end
    }
    eligible_failures = [f for f in failures if f["deployment"] in ids]
    intervals = [
        (
            timestamp(recovered[f["id"]]["timestamp"]) - timestamp(f["timestamp"])
        ).total_seconds()
        for f in eligible_failures
        if f["id"] in recovered
    ]
    tasks = [
        t
        for t in snapshot["tasks"]
        if t["completed_at"] and start <= timestamp(t["completed_at"]) < end
    ]
    observed = snapshot["settings"]["incidents_supplied"]
    return {
        "start": iso(start),
        "end_exclusive": iso(end),
        "deployments": len(deploys),
        "changes": sum(cohorts.values()),
        "change_cohorts": cohorts,
        "lead_time_seconds": median(durations),
        "negative_durations": negative,
        "deployment_frequency_per_day": ratio(
            len(deploys), (end - start).total_seconds() / 86400
        ),
        "change_fail_rate": ratio(
            len({f["deployment"] for f in eligible_failures}), len(deploys)
        )
        if observed
        else None,
        "failed_deployment_recovery_seconds": median(intervals) if observed else None,
        "deployment_rework_rate": ratio(
            len({tag for f in failures for tag in f["rework"] if tag in ids}),
            len(deploys),
        )
        if observed
        else None,
        "incident_coverage": "confirmed observed evidence"
        if observed
        else "unavailable",
        "unresolved_incidents": sum(f["id"] not in recovered for f in eligible_failures)
        if observed
        else None,
        "agent_metrics": agent_metrics(tasks),
        "task_cohorts": {
            cohort: agent_metrics([t for t in tasks if t["cohort"] == cohort])
            for cohort in cohorts
        },
    }


def render(snapshot: dict) -> str:
    end = timestamp(snapshot["cutoff"])
    weeks = snapshot["settings"]["weeks"]
    start = end - timedelta(weeks=weeks)
    total = aggregate(snapshot, start, end)
    weekly = [
        aggregate(snapshot, start + timedelta(weeks=i), start + timedelta(weeks=i + 1))
        for i in range(weeks)
    ]
    lines = [
        START,
        "## Generated baseline",
        "",
        f"Schema: `{SCHEMA}`. Implementation-run UTC date: {snapshot['run_date']}.",
        f"Source revision: `{snapshot['revision']}`.",
        f"Window: {iso(start)} to {iso(end)} (exclusive).",
        "",
        "Effective settings and evidence inventory are embedded below. Incident coverage: "
        + total["incident_coverage"]
        + ".",
        "",
        "| UTC week start | Deployments | Changes | Lead median seconds (n) | Fail deployments / deployments | Recovery median seconds (n) | Rework deployments / deployments |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    def cell(metric: dict | None) -> str:
        if metric is None:
            return "unavailable"
        if "count" in metric:
            return (
                f"{metric['value']} ({metric['count']})"
                if metric["value"] is not None
                else "unavailable (0)"
            )
        return (
            f"{metric['numerator']} / {metric['denominator']}"
            if metric["value"] is not None
            else f"unavailable ({metric['numerator']} / {metric['denominator']})"
        )

    for row in weekly:
        lines.append(
            f"| {row['start'][:10]} | {row['deployments']} | {row['changes']} | {cell(row['lead_time_seconds'])} | {cell(row['change_fail_rate'])} | {cell(row['failed_deployment_recovery_seconds'])} | {cell(row['deployment_rework_rate'])} |"
        )
    lines += [
        "",
        "```mermaid",
        "xychart-beta",
        '  title "Release proxy deployments per week"',
        "  x-axis [" + ", ".join('"' + r["start"][:10] + '"' for r in weekly) + "]",
        '  y-axis "Deployments" 0 --> '
        + str(max(1, max(r["deployments"] for r in weekly))),
        "  line [" + ", ".join(str(r["deployments"]) for r in weekly) + "]",
        "```",
        "",
        "Unavailable metrics have gaps: no numeric series is drawn for them.",
        "Plan approval without edits: unavailable. Supervision burden: unavailable. Escape rate: unavailable.",
        "",
        "### Totals and coverage",
        "",
        "```json",
        json.dumps(total, sort_keys=True, indent=2),
        "```",
        "",
        "### Normalized evidence snapshot",
        "",
        "Reproduce the generated block offline with `render(snapshot)` from `scripts/metrics.py`.",
        "Local unfinished observations are retained in this snapshot and excluded from completion metrics.",
        "",
        "```json",
        json.dumps(snapshot, sort_keys=True, indent=2),
        "```",
        END,
    ]
    result = "\n".join(lines) + "\n"
    if len(result.encode("utf-8")) > MAX_BYTES:
        raise ValueError("baseline exceeds bounded publication size")
    return result


def publish(output: Path, block: str) -> None:
    original = output.read_text(encoding="utf-8") if output.exists() else ""
    if original.count(START) != original.count(END) or original.count(START) > 1:
        raise ValueError("invalid baseline markers")
    if START in original:
        left, rest = original.split(START)
        middle, right = rest.split(END)
        if END in left or START in middle:
            raise ValueError("reversed baseline markers")
        result = left + block.rstrip("\n") + right
    else:
        result = (
            original
            + ("\n" if original and not original.endswith("\n") else "")
            + block
        )
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=output.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(result)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build(
    repo: Path,
    revision: str,
    cutoff: datetime,
    weeks: int,
    prefix: str,
    incidents: Path | None,
    task_root: Path | None,
    run_date: str,
) -> dict:
    revision, changes, deploys, commits = git_events(repo, revision, prefix, cutoff)
    failures, recoveries = incident_events(incidents, deploys)
    tasks = task_events(repo, revision, commits, task_root)
    return {
        "schema": SCHEMA,
        "revision": revision,
        "run_date": run_date,
        "cutoff": iso(cutoff),
        "settings": {
            "weeks": weeks,
            "tag_prefix": prefix,
            "incidents_supplied": incidents is not None,
            "local_tasks_supplied": task_root is not None,
        },
        "inventory": {
            "reachable_changes": len(changes),
            "eligible_tags": len(deploys),
            "incidents": len(failures),
            "recoveries": len(recoveries),
            "tasks": len(tasks),
            "local_unfinished_tasks": sum(t["completed_at"] is None for t in tasks),
        },
        "changes": [asdict(c) for c in changes],
        "deploys": [asdict(d) for d in deploys],
        "failures": [asdict(f) for f in failures],
        "recoveries": [asdict(r) for r in recoveries],
        "tasks": tasks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("report", "publish"):
        command = sub.add_parser(name)
        command.add_argument("--repo", type=Path, default=Path.cwd())
        command.add_argument("--revision", default="HEAD")
        command.add_argument("--cutoff", required=name == "publish")
        command.add_argument("--weeks", type=int)
        command.add_argument("--tag-prefix")
        command.add_argument("--incidents", type=Path)
        command.add_argument("--task-root", type=Path)
        if name == "publish":
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        now = datetime.now(UTC)
        cutoff = timestamp(
            args.cutoff + "T00:00:00Z"
            if args.cutoff and re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.cutoff)
            else args.cutoff or now
        )
        settings = policy.load(args.repo, Path.home())["values"]
        weeks = args.weeks if args.weeks is not None else settings["metrics.weeks"]
        prefix = (
            args.tag_prefix
            if args.tag_prefix is not None
            else settings["metrics.tag_prefix"]
        )
        incidents = args.incidents
        if (
            incidents is None
            and settings["metrics.enabled"]
            and settings["metrics.incident_path"]
        ):
            incidents = args.repo / settings["metrics.incident_path"]
        if not 1 <= weeks <= 52 or not prefix or any(c in prefix for c in "\n\r\0"):
            raise ValueError("invalid weeks or tag prefix")
        if args.command == "publish":
            output = args.output.resolve()
            if (
                output.suffix != ".md"
                or ".git" in output.parts
                or "tasks" in output.parts
                or (incidents and output == incidents.resolve())
                or (args.task_root and output.is_relative_to(args.task_root.resolve()))
            ):
                raise ValueError("output must be a report outside evidence inputs")
        snapshot = build(
            args.repo,
            args.revision,
            cutoff,
            weeks,
            prefix,
            incidents,
            args.task_root,
            now.date().isoformat(),
        )
        block = render(snapshot)
        if args.command == "publish":
            publish(output, block)
        else:
            print(block, end="")
        return 0
    except (ValueError, OSError, UnicodeError) as exc:
        print("metrics: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
