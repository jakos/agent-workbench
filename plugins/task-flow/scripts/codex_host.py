# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Run shared Task Flow commands after the Codex pre-tool hook authorizes dispatch."""

import json
import os
import re
import runpy
import sys
import time
from pathlib import Path

import phase_gate
from codex_hook import MAX_TICKET_AGE, PROTECTED_SCRIPTS, SCRIPT_DIR, TICKETS

SCRIPTS = PROTECTED_SCRIPTS | {"doctor.py", "models.py", "policy.py", "task_list.py"}


def consume(project: Path, argv: list[str], token: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ValueError("invalid Codex hook ticket")
    directory = project / ".work" / TICKETS
    if directory.is_symlink() or directory.is_junction():
        raise ValueError("hook ticket directory must not be linked")
    path = directory / f"{token}.json"
    if path.is_symlink():
        raise ValueError("hook ticket must not be linked")
    value = json.loads(path.read_bytes())
    path.unlink()  # A second consumer must fail before dispatch.
    if (
        value["argv"] != argv
        or not 0 <= time.time() - value["issued"] <= MAX_TICKET_AGE
    ):
        raise ValueError(
            "Codex hook ticket does not match this invocation or has expired"
        )


def main(argv: list[str]) -> int:
    try:
        if len(argv) < 3 or argv[-2] != "--hook-ticket" or argv[0] not in SCRIPTS:
            raise ValueError(
                "Task Flow requires its trusted Codex pre-tool hook; review it in /hooks"
            )
        cwd = Path.cwd().resolve()
        project = phase_gate.project_from_cwd(cwd)
        consume(project, argv[:-2], argv[-1])
    except (ValueError, TypeError, OSError, KeyError) as exc:
        print(f"Codex Task Flow refused: {exc}", file=sys.stderr)
        return 2
    os.environ["TASK_FLOW_HOST"] = "codex"
    script = SCRIPT_DIR / argv[0]
    sys.argv = [str(script), *argv[1:-2]]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
