# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Strict, read-only repository autonomy policy. No file preserves legacy answers."""

import argparse
import fnmatch
import json
import math
import re
import sys
import tomllib
from pathlib import Path

import risk
from role_contracts import _sections

CONFIG = ".claude/task-flow.policy.toml"
DEFAULTS = {
    "plan_approval": "ask",
    "max_rounds": 3,
    "no_progress_stop": True,
    "push": "ask",
    "pr": "ask",
    "merge": "ask",
    "release": "ask",
    "isolation_required": False,
    "budget_tokens": None,
    "budget_usd": None,
    "risk_paths": [],
    "autopilot.mode": "semi",
    "metrics.enabled": False,
    "metrics.tag_prefix": "task-flow-",
    "metrics.weeks": 7,
    "metrics.incident_path": "",
}
PRESETS = {
    "supervised": {},
    "gated": {"plan_approval": "auto-low-risk", "push": "auto", "pr": "auto"},
    "autonomous": {
        "plan_approval": "auto",
        "push": "auto",
        "pr": "auto",
        "merge": "auto-low-risk",
        "isolation_required": True,
        "autopilot.mode": "full",
    },
}
DOMAINS = {
    "plan_approval": ("ask", "auto-low-risk", "auto"),
    "push": ("ask", "auto"),
    "pr": ("ask", "auto"),
    "merge": ("ask", "auto-low-risk", "auto"),
    "release": ("ask", "auto"),
    "autopilot.mode": ("semi", "full"),
}


class ConfigError(ValueError):
    """A refusal naming keys, never echoing untrusted values."""


def parse(text: str) -> dict:
    try:
        data = tomllib.loads(text)
    except (ValueError, TypeError):
        raise ConfigError("policy: invalid TOML") from None
    # Only display bounded identifier keys; never echo arbitrary paths or values.
    unknown = set(data) - (
        {key for key in DEFAULTS if "." not in key} | {"preset", "autopilot", "metrics"}
    )
    if unknown:
        key = min(unknown)
        label = key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key) else "policy"
        raise ConfigError(f"{label}: unknown key or table (see templates/policy.toml)")
    if "autopilot" in data:
        table = data.pop("autopilot")
        if not isinstance(table, dict) or set(table) - {"mode"}:
            raise ConfigError("autopilot: expected only mode")
        if "mode" in table:
            data["autopilot.mode"] = table["mode"]
    if "metrics" in data:
        table = data.pop("metrics")
        if not isinstance(table, dict) or set(table) - {
            "enabled",
            "tag_prefix",
            "weeks",
            "incident_path",
        }:
            raise ConfigError("metrics: unknown key or table")
        data.update({"metrics." + key: value for key, value in table.items()})
    for key, value in data.items():
        valid = True
        if key == "preset":
            valid = isinstance(value, str) and value in PRESETS
        elif key in DOMAINS:
            valid = isinstance(value, str) and value in DOMAINS[key]
        elif key == "max_rounds":
            valid = type(value) is int and 1 <= value <= 3
        elif key == "no_progress_stop":
            if value is not True:
                raise ConfigError(
                    "no_progress_stop: must be true; workflow stop is mandatory"
                )
        elif key == "isolation_required":
            valid = type(value) is bool
        elif key == "budget_tokens":
            valid = type(value) is int and value > 0
        elif key == "budget_usd":
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        elif key == "risk_paths":
            valid = isinstance(value, list) and all(
                isinstance(item, str) and risk.safe_path(item) for item in value
            )
        elif key == "metrics.enabled":
            valid = type(value) is bool
        elif key == "metrics.weeks":
            valid = type(value) is int and 1 <= value <= 52
        elif key == "metrics.tag_prefix":
            valid = (
                isinstance(value, str)
                and bool(value)
                and not any(c in value for c in "\n\r\0")
            )
        elif key == "metrics.incident_path":
            valid = isinstance(value, str) and (
                value == ""
                or risk.safe_path(value)
                and not any(c in value for c in "*?[]")
            )
        if not valid:
            raise ConfigError(
                f"{key}: invalid type or value (see templates/policy.toml)"
            )
    return data


def load(project: Path, home: Path) -> dict:
    values = {**DEFAULTS, "risk_paths": []}
    sources = dict.fromkeys(values, "default")
    files = []
    preset = "supervised"
    for source, path in (("user", home / CONFIG), ("project", project / CONFIG)):
        if not path.exists():
            continue
        try:
            data = parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            raise ConfigError(
                f"{source} policy: cannot read UTF-8 configuration"
            ) from None
        files.append({"source": source, "path": str(path.resolve())})
        incoming = {}
        origins = {}
        if "preset" in data:
            preset = data.pop("preset")
            incoming = {**DEFAULTS, **PRESETS[preset]}
            origins = dict.fromkeys(incoming, f"preset:{preset}")
        incoming.update(data)
        origins.update(dict.fromkeys(data, source))
        for key, value in incoming.items():
            previous = values[key]
            if key == "isolation_required" and previous and not value:
                continue
            if (
                key in ("max_rounds", "budget_tokens", "budget_usd")
                and previous is not None
                and (value is None or previous < value)
            ):
                continue
            values[key], sources[key] = value, origins[key]
    return {
        "schema": "policy/1",
        "mode": "configured" if files else "legacy",
        "preset": preset if files else "legacy",
        "values": values,
        "sources": sources,
        "files": files,
    }


def effective(policy: dict, isolation: str = "none") -> dict:
    if isolation not in ("none", "active"):
        raise ConfigError("isolation: expected active or none")
    if isolation != "active" and (
        policy["preset"] == "autonomous" or policy["values"]["isolation_required"]
    ):
        raise ConfigError(
            "isolation: active backend required; missing isolation backend"
        )
    return policy


def plan_risk(plan_path: Path, policy: dict) -> str:
    try:
        text = plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ConfigError("plan: cannot read UTF-8 plan") from None
    sections, _ = _sections(text)
    ratings = [
        line
        for line in sections.get("Risks / open questions", [])
        if line.startswith("plan-risk:")
    ]
    if len(ratings) != 1 or ratings[0] not in [f"plan-risk: {r}" for r in risk.LEVELS]:
        return "high"
    paths = set()
    for line in sections.get("Profiles", []):
        if ":" in line:
            paths.update(
                p.strip().strip("`").replace("\\", "/")
                for p in line.split(":", 1)[1].split(",")
                if p.strip()
            )
    steps = sections.get("Steps", [])
    for line in steps:
        paths.update(
            p
            for p in re.findall(r"`([^`]+)`", line)
            if "/" in p or "\\" in p or re.search(r"\.[A-Za-z0-9*?]+$", p)
        )
        paths.update(re.findall(r"(?<![\w/])(?:[\w.*-]+/)+[\w.*?/-]+", line))
    paths = {p.replace("\\", "/") for p in paths}
    sensitive = any(
        risk.HIGH_PATH.search(p)
        or p.startswith(".claude/task-flow")
        or any(
            fnmatch.fnmatchcase(p, pattern)
            for pattern in policy["values"]["risk_paths"]
        )
        for p in paths
    )
    deterministic = (
        "high"
        if sensitive
        else "medium"
        if sum(bool(re.match(r"^\d+\. ", line)) for line in steps) >= 10
        or len(paths) >= 15
        else "low"
    )
    return max((deterministic, ratings[0][11:]), key=risk.LEVELS.index)


def allow_auto(policy: dict, rating: str) -> bool:
    if rating not in risk.LEVELS:
        raise ConfigError("plan-risk: expected low, medium or high")
    return (
        policy["mode"] == "legacy"
        or policy["values"]["plan_approval"] == "auto"
        or (policy["values"]["plan_approval"] == "auto-low-risk" and rating == "low")
    )


def finish_settings(project: Path) -> dict:
    path = project / ".claude/task-flow.md"
    if not path.exists():
        return {}
    try:
        sections, _ = _sections(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        raise ConfigError("Finish: cannot read UTF-8 project configuration") from None
    settings = {}
    for line in sections.get("Finish", []):
        match = re.match(r"^- (push|pr): (yes|auto|ask)\b", line)
        if match:
            settings[match[1]] = "auto" if match[2] in ("yes", "auto") else "ask"
    return settings


def review_risk(task_dir: Path | None) -> str:
    if task_dir is None:
        return "unavailable"
    try:
        status = (task_dir / "status.md").read_text(encoding="utf-8")
        rounds = re.findall(r"^- round:(.*)$", status, re.MULTILINE)
        if len(rounds) != 1 or rounds[0] not in (" 1", " 2", " 3"):
            return "invalid"
        text = (task_dir / f"review-{rounds[0].strip()}.md").read_text(encoding="utf-8")
        attention = risk.parse_attention(text)
        return attention["risk"] if attention else "unavailable"
    except (OSError, UnicodeError, risk.RiskError):
        return "invalid"


def decide(
    action: str,
    policy: dict,
    project: Path,
    plan: Path | None = None,
    task_dir: Path | None = None,
    isolation: str = "none",
) -> dict:
    effective(policy, isolation)
    if action not in ("plan-approval", "push", "pr", "merge", "release"):
        raise ConfigError("action: unsupported decision")
    setting = policy["values"][action.replace("-", "_")]
    if policy["mode"] == "legacy":
        setting = finish_settings(project).get(action, "ask")
    rating = None
    if setting == "auto-low-risk":
        rating = (
            plan_risk(plan, policy)
            if action == "plan-approval" and plan
            else review_risk(task_dir)
            if action == "merge"
            else "unavailable"
        )
    allowed = setting == "auto" or (setting == "auto-low-risk" and rating == "low")
    reason = (
        "auto (explicit)"
        if action == "release" and allowed
        else "auto"
        if allowed
        else "ask"
    )
    return {"action": action, "allowed": allowed, "decision": reason, "risk": rating}


def table(policy: dict) -> str:
    lines = [f"{policy['mode']}: {policy['preset']}"]
    for key, value in policy["values"].items():
        label = (
            "auto (explicit)"
            if key == "release" and value == "auto"
            else json.dumps(value)
        )
        lines.append(f"{key} = {label} [{policy['sources'][key]}]")
    return "; ".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "plan-risk", "allow-auto", "decide", "validate"):
        command = sub.add_parser(name)
        command.add_argument("--project", type=Path, default=Path.cwd())
        command.add_argument("--format", choices=("table", "json"), default="table")
        command.add_argument("--isolation", choices=("active", "none"), default="none")
        if name in ("plan-risk", "allow-auto", "decide"):
            command.add_argument("--plan", type=Path, required=name == "plan-risk")
        if name == "allow-auto":
            command.add_argument("--scope", choices=("plan-approval",), required=True)
            command.add_argument("--plan-risk", choices=risk.LEVELS)
        if name == "decide":
            command.add_argument(
                "action", choices=("plan-approval", "push", "pr", "merge", "release")
            )
            command.add_argument("--task-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        policy = effective(load(args.project, Path.home()), args.isolation)
        result = dict(policy)
        code = 0
        if args.command == "plan-risk":
            result["risk"] = plan_risk(args.plan, policy)
        elif args.command == "allow-auto":
            if not args.plan and not args.plan_risk:
                raise ConfigError("plan-risk: supply --plan or --plan-risk")
            rating = plan_risk(args.plan, policy) if args.plan else args.plan_risk
            result.update({"risk": rating, "allowed": allow_auto(policy, rating)})
            result["decision"] = "auto" if result["allowed"] else "ask"
            code = 0 if result["allowed"] else 1
        elif args.command == "decide":
            result.update(
                decide(
                    args.action,
                    policy,
                    args.project,
                    args.plan,
                    args.task_dir,
                    args.isolation,
                )
            )
            code = 0 if result["allowed"] else 1
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        else:
            print(table(policy))
            if "decision" in result:
                print(
                    result["decision"]
                    + (f"; risk: {result['risk']}" if result.get("risk") else "")
                )
            elif "risk" in result:
                print("risk: " + result["risk"])
        return code
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
