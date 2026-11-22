# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Adapt Codex shell and patch events to the shared workflow guard."""

import json
import re
import secrets
import shlex
import sys
import time
from contextlib import suppress
from pathlib import Path

import phase_gate
import rules

STATE_FILES = {
    "status.md",
    "gates.jsonl",
    "run.md",
    "epic.md",
    ".implementer-host.json",
}
PROTECTED_PREFIXES = (".operation-", ".gate-lock-", ".gate-context-")
SCRIPT_DIR = Path(__file__).resolve().parent
HOST = SCRIPT_DIR / "codex_host.py"
TICKETS = ".codex-hook-tickets"
MAX_TICKET_AGE = 120
PROTECTED_SCRIPTS = frozenset(
    {
        "autopilot.py",
        "epics.py",
        "gates.py",
        "implement.py",
        "phase_gate.py",
        "prune_task.py",
        "publish_task.py",
        "release.py",
        "role_run.py",
        "sdlc_status.py",
        "task_record.py",
    }
)
DIRECT_SCRIPT = re.compile(
    r"""(?:^|[;&|\r\n])\s*(?:&\s*)?(?:(?:uv(?:\.exe)?\s+run\s+(?:--quiet\s+)?--script|python(?:3)?(?:\.exe)?)\s+)(?:"([^"]+)"|'([^']+)'|(\S+))""",
    re.IGNORECASE,
)


def protected(path: str, cwd: Path, project: Path) -> bool:
    if rules.witness_write(path, cwd):
        return True
    candidate = (cwd / path).resolve()
    work = project / ".work"
    if not candidate.is_relative_to(work):
        return False
    parts = candidate.relative_to(work).parts
    return bool(
        not parts
        or TICKETS in parts
        or candidate.name in STATE_FILES
        and "worktree" not in parts
        or candidate.name.startswith(PROTECTED_PREFIXES)
    )


def host_argv(command: str, cwd: Path) -> list[str] | None:
    if "codex_host.py" not in command:
        return None
    if re.search(r"[;&|><\x60$\r\n]", command):
        raise ValueError("use a literal standalone uv command for Codex Task Flow")
    argv = shlex.split(command, posix=False)
    argv = [
        word[1:-1] if word[:1] in {"'", '"'} and word[-1:] == word[:1] else word
        for word in argv
    ]
    if (
        argv[:4] != ["uv", "run", "--quiet", "--script"]
        or len(argv) < 6
        or (cwd / argv[4]).resolve() != HOST
        or "--hook-ticket" in argv
    ):
        raise ValueError(
            "use the installed Codex Task Flow launcher with literal arguments"
        )
    return argv[5:]


def issue_ticket(project: Path, argv: list[str]) -> str:
    token = secrets.token_hex(32)
    directory = project / ".work" / TICKETS
    if directory.is_symlink() or directory.is_junction():
        raise ValueError("hook ticket directory must not be linked")
    directory.mkdir(parents=True, exist_ok=True)
    # Abandoned tickets cannot accumulate across sessions.
    for path in directory.glob("*.json"):
        with suppress(FileNotFoundError):
            if (
                re.fullmatch(r"[a-f0-9]{64}\.json", path.name)
                and time.time() - path.stat().st_mtime > MAX_TICKET_AGE
            ):
                path.unlink(missing_ok=True)
    with (directory / f"{token}.json").open("x", encoding="utf-8") as file:
        json.dump({"argv": argv, "issued": time.time()}, file)
    return token


def decision(payload: dict) -> dict | None:
    if payload.get("hook_event_name") != "PreToolUse":
        raise ValueError("unexpected Codex hook event")
    cwd = Path(payload["cwd"]).resolve()
    project = phase_gate.project_from_cwd(cwd)
    command = payload["tool_input"]["command"]
    if not isinstance(command, str):
        raise TypeError("missing Codex tool command")
    tool = payload["tool_name"]
    if tool == "apply_patch":
        paths = re.findall(
            r"(?m)^\*\*\* (?:Add File:|Update File:|Delete File:|Move to:) (.+)$",
            command,
        )
        if not paths or any(
            protected(path.rstrip("\r"), cwd, project) for path in paths
        ):
            raise ValueError(
                "patch targets protected Task Flow state or has no recognized paths"
            )
    elif tool == "Bash":
        argv = host_argv(command, cwd)
        if argv is not None:
            token = issue_ticket(project, argv)
            return {
                "permissionDecision": "allow",
                "updatedInput": {"command": f"{command} --hook-ticket {token}"},
            }
        if "codex_hook.py" in command:
            raise ValueError("Codex must invoke the Task Flow hook")
        for match in DIRECT_SCRIPT.finditer(command):
            program = next(value for value in match.groups() if value)
            if (
                program.replace("\\", "/").rsplit("/", 1)[-1].casefold()
                in PROTECTED_SCRIPTS
            ):
                raise ValueError(
                    "use the hook-authorized launcher for protected Task Flow scripts"
                )
        if (
            (project / ".work") in cwd.parents
            and re.search(r"(?i)\bgit\b.*\b(?:commit|worktree)\b", command)
            and not re.search(r"(?i)\b(?:list|show|log|diff)\b", command)
        ):
            raise ValueError(
                "use the shared Task Flow command for task commits and worktrees"
            )
        reason = phase_gate.hook(payload)
        if reason:
            raise ValueError(reason)
        if any(
            word in command.casefold()
            for word in (*STATE_FILES, TICKETS, *PROTECTED_PREFIXES)
        ) and re.search(
            r"(?i)\b(?:python\w*|node|rm|remove-item|set-content|add-content|out-file|mv|cp|sed|tee)\b|>",
            command,
        ):
            raise ValueError("use Task Flow commands for protected workflow state")
    return None


def main() -> int:
    try:
        result = decision(json.load(sys.stdin))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"task-flow: {exc}", file=sys.stderr)
        return 2
    if result is not None:
        print(
            json.dumps(
                {"hookSpecificOutput": {"hookEventName": "PreToolUse", **result}}
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
