# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Read-only, base-pinned review risk assessment. Signals are advisory heuristics."""

import argparse
import difflib
import fnmatch
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

LEVELS = ("low", "medium", "high")
CONFIG = ".claude/task-flow.risk.toml"
HIGH_PATH = re.compile(
    r"(?:^|/)(?:security|auth|authentication|authorization|hooks?|ci|migrations?|deploy(?:ment)?s?|release)(?:[./_-]|$)"
    r"|(?:^|/)[^/]*[._-]release(?:[._-]|$)"
    r"|(?:^|/)(?:rules\.py|release\.py|Dockerfile|Containerfile|Jenkinsfile)$"
    r"|(?:^|/)(?:\.github/workflows|\.gitlab-ci|\.circleci|helm|k8s|kubernetes)(?:[./]|$)",
    re.IGNORECASE,
)
DEPENDENCY = re.compile(
    r"(?:^|/)(?:requirements[^/]*\.txt|pyproject\.toml|uv\.lock|package(?:-lock)?\.json|Cargo\.(?:toml|lock)|go\.(?:mod|sum)|pom\.xml|build\.gradle)$"
)
TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|test_[^/]+)(?:/|\.|$)|(?:_test\.py|\.test\.[^/]+)$"
)
ASSERTION = re.compile(r"\b(?:assert\b|self\.assert\w+|expect\s*\()")
SKIP = re.compile(r"\b(?:skip|skipif|xfail|expectedFailure|unittest\.skip)\b")
COVERAGE = re.compile(
    r"(fail[_-]under|coverageThreshold|(?:branches|functions|lines|statements)\s*[:=])[^0-9]*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
SECRET = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|(?:gh[pousr]_|sk-)[A-Za-z0-9_-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
    r"|(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*[\"']([^\"'\r\n]{8,})[\"']",
    re.IGNORECASE,
)


class RiskError(ValueError):
    """Refused input; messages never contain inspected values."""


def safe_path(value: str) -> bool:
    return bool(value) and not (
        value.startswith(("/", "~"))
        or "\\" in value
        or ":" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def parse_config(text: str | None) -> dict:
    if text is None:
        return {"enabled": False, "paths": []}
    try:
        data = tomllib.loads(text)
    except (ValueError, TypeError):
        raise RiskError("invalid risk TOML") from None
    if set(data) - {"risk", "paths"} or not isinstance(data.get("risk"), dict):
        raise RiskError("risk config requires [risk] and only optional paths")
    if set(data["risk"]) != {"enabled"} or type(data["risk"]["enabled"]) is not bool:
        raise RiskError("risk.enabled must be boolean; unknown keys refused")
    paths = data.get("paths", [])
    if not isinstance(paths, list):
        raise RiskError("paths must be an array of tables")
    for rule in paths:
        if (
            not isinstance(rule, dict)
            or set(rule) != {"pattern", "level"}
            or not isinstance(rule["pattern"], str)
            or not safe_path(rule["pattern"])
            or rule["level"] not in LEVELS
        ):
            raise RiskError("invalid risk path rule")
    return {"enabled": data["risk"]["enabled"], "paths": paths}


def git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "--no-pager", "-C", str(repo), *args],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RiskError("cannot read required Git evidence") from None
    if result.returncode:
        raise RiskError("cannot read required Git evidence")
    return result.stdout


def metadata(repo: Path, task_dir: Path, worktree: Path, round_number: int) -> str:
    repo, task_dir, worktree = repo.resolve(), task_dir.resolve(), worktree.resolve()
    if task_dir.parent != repo / ".work" or round_number < 1:
        raise RiskError("invalid task directory or round")
    try:
        status = (task_dir / "status.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise RiskError("cannot read task metadata") from None
    fields = {}
    for line in status.split("## ", 1)[0].splitlines():
        if line.startswith("- "):
            key, sep, value = line[2:].partition(":")
            if sep:
                if key in fields:
                    raise RiskError("duplicate task metadata")
                fields[key] = value.strip()
    selected = fields.get("worktree")
    if selected is None or fields.get("round") != str(round_number):
        raise RiskError("task round or worktree mismatch")
    expected = repo if selected == "none" else (repo / selected).resolve()
    if expected != worktree:
        raise RiskError("task worktree mismatch")
    base = fields.get("base", "")
    if not base or base.startswith("-"):
        raise RiskError("missing task base")
    return (
        git(worktree, "rev-parse", "--verify", f"{base}^{{commit}}")
        .decode("ascii")
        .strip()
    )


def base_config(worktree: Path, base: str) -> dict:
    exists = git(worktree, "ls-tree", "-z", base, "--", CONFIG)
    if not exists:
        return parse_config(None)
    try:
        return parse_config(git(worktree, "show", f"{base}:{CONFIG}").decode("utf-8"))
    except UnicodeError:
        raise RiskError("risk config is not UTF-8") from None


def task_config(repo: Path, task_dir: Path, worktree: Path, round_number: int) -> dict:
    return base_config(worktree, metadata(repo, task_dir, worktree, round_number))


def parse_attention(text: str) -> dict | None:
    # Import lazily to share fence-aware heading parsing without an import cycle.
    from role_contracts import _sections

    sections, order = _sections(text)
    if "Human attention" not in order:
        return None
    index = order.index("Human attention")
    if (
        order.count("Human attention") != 1
        or index + 1 >= len(order)
        or not order[index + 1].startswith("Verdict: ")
    ):
        raise RiskError("Human attention must occur once immediately before verdict")
    lines = [line for line in sections["Human attention"] if line.strip()]
    if (
        len(lines) < 3
        or not re.fullmatch(r"risk: (low|medium|high)", lines[0])
        or not re.fullmatch(r"why: \S.*", lines[1])
    ):
        raise RiskError("attention requires one risk, one nonempty why and entries")
    entries = []
    if lines[2:] != ["- none"]:
        if not 1 <= len(lines[2:]) <= 5:
            raise RiskError("attention requires one to five entries or none")
        for line in lines[2:]:
            match = re.fullmatch(r"- (.+):([1-9][0-9]*) — (\S.*)", line)
            if not match or not safe_path(match[1]):
                raise RiskError("unsafe or malformed attention location")
            entries.append({"path": match[1], "line": int(match[2]), "what": match[3]})
    return {"risk": lines[0][6:], "why": lines[1][5:], "attention": entries}


def validate_attention(text: str, assessment: dict | None = None) -> dict | None:
    attention = parse_attention(text)
    if assessment and assessment["enabled"]:
        if attention is None:
            raise RiskError("enabled review requires Human attention")
        if LEVELS.index(attention["risk"]) < LEVELS.index(assessment["floor"]):
            raise RiskError("review risk is below the deterministic floor")
        for entry in attention["attention"]:
            bounds = assessment["files"].get(entry["path"])
            if bounds is None or entry["line"] > max(1, bounds):
                raise RiskError(
                    "attention must reference a reviewed current or deleted base line"
                )
    return attention


def inspect_changes(
    changes: list[tuple[str, str | None, bytes | None, bytes | None]], config: dict
) -> dict:
    """Assess complete old/current byte pairs; a rename carries both path names."""
    result = {
        "schema": "review-risk/1",
        "enabled": config["enabled"],
        "floor": None,
        "reasons": [],
        "locations": [],
        "files": {},
        "changed_lines": 0,
        "touched_files": len(changes),
    }
    if not config["enabled"]:
        return result
    floor = 0

    def signal(level: str, code: str, path: str | None = None, line: int = 1) -> None:
        nonlocal floor
        floor = max(floor, LEVELS.index(level))
        if code not in result["reasons"]:
            result["reasons"].append(code)
        if path and safe_path(path):
            location = {"path": path, "line": line, "reason": code}
            if location not in result["locations"]:
                result["locations"].append(location)

    for path, old_path, old, new in changes:
        names = list(dict.fromkeys([old_path or path, path]))
        if not all(safe_path(name) for name in names):
            signal("high", "incomplete-analysis")
            continue
        decoded = []
        for name, content in ((old_path or path, old), (path, new)):
            try:
                if content is None:
                    raise UnicodeError()
                value = content.decode("utf-8")
                if "\0" in value:
                    raise UnicodeError()
                decoded.append(value.splitlines())
            except UnicodeError:
                decoded.append([])
                signal("high", "incomplete-analysis", name)
        before, after = decoded
        for name in names:
            result["files"][name] = (
                len(after) if name == path and new != b"" else len(before)
            )
            if HIGH_PATH.search(name):
                signal("high", "sensitive-path", name)
            for rule in config["paths"]:
                if fnmatch.fnmatchcase(name, rule["pattern"]):
                    signal(rule["level"], "custom-path", name)
        if TEST_PATH.search(old_path or path) and old and not new:
            signal("medium", "deleted-test", old_path or path)
        for tag, i, j, a, b in difflib.SequenceMatcher(
            None, before, after, autojunk=False
        ).get_opcodes():
            if tag == "equal":
                continue
            result["changed_lines"] += j - i + b - a
            removed, added = before[i:j], after[a:b]
            if DEPENDENCY.search(path) and any(
                line.strip() and not line.lstrip().startswith("#") for line in added
            ):
                signal("medium", "added-dependency", path, a + 1)
            if TEST_PATH.search(path):
                if any(ASSERTION.search(line) for line in removed):
                    signal("medium", "removed-assertion", old_path or path, i + 1)
                if any(SKIP.search(line) for line in removed + added):
                    signal("medium", "changed-test-skip", path, a + 1)
            old_thresholds = coverage_thresholds(removed)
            new_thresholds = coverage_thresholds(added)
            if any(
                key not in new_thresholds or new_thresholds[key] < value
                for key, value in old_thresholds.items()
            ):
                signal("medium", "reduced-coverage", path, a + 1)
            for offset, line in enumerate(added):
                if SECRET.search(line):
                    signal("high", "added-secret-like-value", path, a + offset + 1)
    if result["changed_lines"] >= 1000 or len(changes) >= 30:
        signal("high", "large-diff")
    elif result["changed_lines"] >= 300 or len(changes) >= 10:
        signal("medium", "large-diff")
    result["floor"] = LEVELS[floor]
    for location in result["locations"]:
        location["line"] = min(
            location["line"], max(1, result["files"].get(location["path"], 0))
        )
    result["locations"].sort(
        key=lambda item: (item["path"], item["line"], item["reason"])
    )
    return result


def coverage_thresholds(lines: list[str]) -> dict[str, float]:
    return {
        re.sub(r"[\s:=_-]", "", match[1].lower()): float(match[2])
        for line in lines
        for match in COVERAGE.finditer(line)
    }


def assess(repo: Path, task_dir: Path, worktree: Path, round_number: int) -> dict:
    base = metadata(repo, task_dir, worktree, round_number)
    config = base_config(worktree, base)
    if not config["enabled"]:
        return inspect_changes([], config)
    raw = git(
        worktree,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--name-status",
        "-z",
        "-M",
        base,
        "--",
    )
    try:
        tokens = raw.decode("utf-8").split("\0")
        changes = []
        index = 0
        while index < len(tokens) - 1:
            status, name = tokens[index : index + 2]
            index += 2
            old_name = name
            if status.startswith(("R", "C")):
                name = tokens[index]
                index += 1
            old = b"" if status == "A" else git(worktree, "show", f"{base}:{old_name}")
            changes.append(
                (
                    name,
                    old_name,
                    old,
                    current_bytes(worktree, name) if status != "D" else b"",
                )
            )
        for name in (
            git(worktree, "ls-files", "--others", "--exclude-standard", "-z")
            .decode("utf-8")
            .split("\0")
        ):
            if name:
                changes.append((name, None, b"", current_bytes(worktree, name)))
    except (UnicodeError, IndexError):
        raise RiskError("incomplete Git path evidence") from None
    result = inspect_changes(changes, config)
    # Empty current files have no base-line attention range. Deleted paths retain it.
    for path, _old_path, _old, new in changes:
        if new == b"" and (worktree / path).is_file() and path in result["files"]:
            result["files"][path] = 0
    return result


def current_bytes(worktree: Path, name: str) -> bytes | None:
    path = worktree / name
    if (
        not safe_path(name)
        or path.is_symlink()
        or not path.resolve().is_relative_to(worktree.resolve())
        or not path.is_file()
    ):
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("assess", "validate"))
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--task-dir", required=True, type=Path)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--round", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        worktree = args.repo if args.worktree == "none" else Path(args.worktree)
        result = assess(args.repo, args.task_dir, worktree, args.round)
        if args.command == "validate" and result["enabled"]:
            text = (args.task_dir / f"review-{args.round}.md").read_text(
                encoding="utf-8"
            )
            from role_contracts import (
                parse_checks,
                parse_verification_commands,
                validate_review,
            )

            plan = (args.task_dir / "plan.md").read_text(encoding="utf-8")
            commands = parse_verification_commands(plan)
            checks = parse_checks(
                (args.task_dir / f"checks-{args.round}.md").read_text(encoding="utf-8"),
                round_number=args.round,
                commands=commands,
            )
            baseline_path = args.task_dir / "checks-0.md"
            baseline = (
                parse_checks(
                    baseline_path.read_text(encoding="utf-8"),
                    round_number=0,
                    commands=commands,
                )
                if baseline_path.exists()
                else None
            )
            validation = validate_review(
                text,
                round_number=args.round,
                plan=plan,
                current=checks,
                baseline=baseline,
                assessment=result,
            )
            if not validation.ok:
                raise RiskError("invalid review")
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (ValueError, OSError, UnicodeError):
        print(
            "risk: refused invalid configuration, metadata, review or incomplete evidence",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
