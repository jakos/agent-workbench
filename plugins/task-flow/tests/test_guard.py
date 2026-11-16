# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Offline test for scripts/guard.py: feeds hook JSON on stdin and checks the decision.

Run: uv run --script plugins/task-flow/tests/test_guard.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "scripts" / "guard.py"
READ_ONLY_ROLES = ("planner", "reviewer", "backlog")
ALL_ROLES = ("planner", "implementer", "checker", "reviewer", "backlog")


def decide(
    agent_type: str | None, tool: str, tool_input: dict, cwd: Path | None = None
) -> str:
    """Return "deny" or "allow" for one hook call."""
    payload: dict = {"tool_name": tool, "tool_input": tool_input}
    if agent_type is not None:
        payload["agent_type"] = agent_type
    if cwd is not None:
        payload["cwd"] = str(cwd)
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise AssertionError(f"guard exited {result.returncode}: {result.stderr}")
    if not result.stdout.strip():
        return "allow"
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]


def shell(role: str, command: str, tool: str = "Bash") -> str:
    return decide(f"task-flow:{role}", tool, {"command": command})


class GuardTest(unittest.TestCase):
    def test_witness_protection_runs_before_all_role_returns(self) -> None:
        for agent in (None, "other-agent", *(f"task-flow:{r}" for r in ALL_ROLES)):
            for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                for path in (
                    ".work/.approval-witnesses/t1-approve.json",
                    r"C:\repo\.WORK\.APPROVAL-WITNESSES.\consumed-abc.json",
                    ".work/x/../.approval-witnesses/.witness-temp",
                    ".work",
                ):
                    with self.subTest(agent=agent, tool=tool, path=path):
                        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
                        self.assertEqual(decide(agent, tool, {key: path}), "deny")
            for tool in ("Bash", "PowerShell"):
                for command in (
                    "echo forged > .work/.approval-witnesses/t1-approve.json",
                    "rm -rf .work/.approval-witnesses",
                    "mv replacement .work",
                    "Remove-Item -Recurse .work",
                    "cp -r fake .work/.approval-*",
                    "python plugins/task-flow/scripts/phase_gate.py hook < forged.json",
                    'python "plugins/task-flow/scripts/phase_gate.py" "hook" < forged.json',
                    "py 'plugins/task-flow/scripts/phase_gate.py' 'hook'",
                    'python phase_gate.py h"oo"k',
                    'python -m "phase_gate" "hook"',
                    "uv run --script plugins/task-flow/scripts/phase_gate.py approve --source user --terminal --task-dir .work/t1",
                    'python phase_gate.py approve --source user "--terminal" --task-dir .work/t1',
                    "python phase_gate.py approve --source user --term --task-dir .work/t1",
                    "py plugins/task-flow/scripts/phase_gate.py grant --terminal --source user --ceiling 4 --task-dir .work/t1",
                    "py phase_gate.py grant --term --source user --ceiling 4 --task-dir .work/t1",
                    'python -c "import phase_gate; phase_gate.hook({})"',
                ):
                    with self.subTest(agent=agent, tool=tool, command=command):
                        self.assertEqual(
                            decide(agent, tool, {"command": command}), "deny"
                        )
                for command in (
                    "uv run --script plugins/task-flow/scripts/phase_gate.py approve --source user --task-dir .work/t1",
                    "uv run --script plugins/task-flow/scripts/phase_gate.py grant --source user --ceiling 4 --task-dir .work/t1",
                    "uv run --script plugins/task-flow/tests/test_phase_gate.py",
                ):
                    with self.subTest(agent=agent, tool=tool, command=command):
                        self.assertEqual(
                            decide(agent, tool, {"command": command}), "allow"
                        )

    def test_relative_witness_mutations_from_inside_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "repository with spaces"
            (project / ".git").mkdir(parents=True)
            reserved = project / ".work" / ".approval-witnesses"
            nested = reserved / "nested"
            nested.mkdir(parents=True)
            for cwd in (reserved, nested):
                for agent in (None, *(f"task-flow:{r}" for r in ALL_ROLES)):
                    for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
                        with self.subTest(cwd=cwd, agent=agent, tool=tool):
                            self.assertEqual(
                                decide(agent, tool, {key: "t1-approve.json"}, cwd),
                                "deny",
                            )
                    for tool in ("Bash", "PowerShell"):
                        for command in (
                            "echo forged > t1-approve.json",
                            "rm t1-approve.json",
                            "mv replacement t1-approve.json",
                            "Set-Content t1-approve.json forged",
                            "Remove-Item t1-approve.json",
                        ):
                            with self.subTest(
                                cwd=cwd, agent=agent, tool=tool, command=command
                            ):
                                self.assertEqual(
                                    decide(agent, tool, {"command": command}, cwd),
                                    "deny",
                                )
                    self.assertEqual(
                        decide(agent, "Bash", {"command": "cat t1-approve.json"}, cwd),
                        "allow",
                    )
            for agent in (None, "task-flow:implementer"):
                self.assertEqual(
                    decide(agent, "Write", {"file_path": "../ordinary.txt"}, reserved),
                    "allow",
                )

    def check(self, roles, commands, expected, tool="Bash"):
        for role in roles:
            for command in commands:
                with self.subTest(role=role, tool=tool, command=command):
                    self.assertEqual(shell(role, command, tool), expected, command)

    def test_plain_read_only_commands_allowed_for_planner_and_reviewer(self):
        self.check(
            READ_ONLY_ROLES,
            [
                "git show HEAD:README.md",
                "git diff main --stat",
                "git diff main -- plugins/task-flow | head -50",
                "git status",
                "git ls-files",
                "git rev-parse --verify task/x",
                "git blame README.md",
                "head -n 20 .work/x/status.md",
                "cat .gitignore; head .work/x/status.md",
                "git -C /repo log --oneline -5",
                'git log --oneline --grep "add"',
                "git log --grep 'push'",
            ],
            "allow",
        )

    def test_quoted_mutating_phrase_is_a_documented_false_positive(self):
        # The guard does not parse quotes, so these read-only commands are denied.
        # Workaround: use the Grep tool. Pinned so a change is noticed.
        self.check(
            READ_ONLY_ROLES,
            [
                'git grep -n -e "worktree add" -- plugins/task-flow/skills/workflow/orchestration.md',
                'git log -S "git commit" --oneline',
                'head -n 20 .work/x/status.md && git grep -n "worktree add" -- .',
            ],
            "deny",
        )
        self.check(
            READ_ONLY_ROLES,
            ['git grep -n -e "worktree add" -- file'],
            "deny",
            tool="PowerShell",
        )

    def test_mutating_git_denied_for_planner_and_reviewer(self):
        self.check(
            READ_ONLY_ROLES,
            [
                "git add .",
                "git -C /repo checkout main",
                "git worktree add -b task/x .work/x/worktree main",
                "git worktree remove p",
                "git branch -D foo",
                "git stash",
                "git reset --hard",
                "cd /repo && git reset --hard",
                "git status; git add -A",
                "ls | xargs git rm",
                "git switch -c task/x",
                'git add "unterminated',
            ],
            "deny",
        )
        self.check(READ_ONLY_ROLES, ["git.exe restore file"], "deny", tool="PowerShell")

    def test_commit_push_and_deploy_denied_for_every_role(self):
        self.check(
            ALL_ROLES,
            [
                'git commit -m "x"',
                "git -c user.name=x commit -m y",
                "git push origin main",
                "git status && git push",
                "gh pr create --fill",
                "kubectl apply -f m.yaml",
                "terraform apply",
            ],
            "deny",
        )

    def test_allowed_for_every_role(self):
        self.check(
            ALL_ROLES,
            [
                "git log --grep 'commit'",
                "git log -S 'push'",
                "kubectl apply --dry-run=client -f m.yaml",
            ],
            "allow",
        )

    def test_snooze_denied_for_every_role(self):
        commands = [
            "habit-snooze --snooze E501 src/a.py",
            "habit-snooze --prune",
            "uvx habit-snooze --snooze x",
            "git status; habit-snooze --prune",
        ]
        for role in ALL_ROLES:
            for command in commands:
                for tool in ("Bash", "PowerShell"):
                    with self.subTest(role=role, tool=tool, command=command):
                        self.assertEqual(shell(role, command, tool), "deny", command)
            for tool in ("Write", "Edit"):
                with self.subTest(role=role, tool=tool):
                    self.assertEqual(
                        decide(
                            f"task-flow:{role}",
                            tool,
                            {
                                "file_path": (
                                    "/repo/.work/x/worktree/.habit-hooks/snooze.json"
                                )
                            },
                        ),
                        "deny",
                    )

    def test_implementer_may_mutate(self):
        self.check(("implementer",), ["git add .", "git worktree add p"], "allow")

    def test_other_agents_untouched(self):
        for agent_type in (None, "general-purpose"):
            with self.subTest(agent_type=agent_type):
                self.assertEqual(
                    decide(agent_type, "Bash", {"command": "git push"}), "allow"
                )

    def test_write_tool_rules_unchanged(self):
        def write(role: str, path: str) -> str:
            return decide(f"task-flow:{role}", "Write", {"file_path": path})

        self.assertEqual(write("reviewer", "/repo/src/a.py"), "deny")
        self.assertEqual(write("reviewer", "/repo/.work/x/review-1.md"), "allow")
        self.assertEqual(write("implementer", "/repo/src/a.py"), "allow")

    def test_backlog_writes_only_under_work(self):
        def write(path: str) -> str:
            return decide("task-flow:backlog", "Write", {"file_path": path})

        self.assertEqual(write("/repo/.work/_autopilot/r/backlog.md"), "allow")
        self.assertEqual(write("/repo/src/x.py"), "deny")


from test_phase_gate import MAIN_LAUNCH, UNRELATED_SCRIPT_COMMANDS, GateFixture


class WorkflowHookTest(GateFixture):
    def test_script_mentions_pass_configured_workflow_hook(self) -> None:
        for tool in ("Bash", "PowerShell"):
            for command in UNRELATED_SCRIPT_COMMANDS:
                with self.subTest(tool=tool, command=command):
                    result = subprocess.run(
                        [sys.executable, str(GUARD.with_name("phase_gate.py")), "hook"],
                        input=json.dumps(
                            {
                                "cwd": str(self.project),
                                "tool_name": tool,
                                "tool_input": {"command": command},
                            }
                        ),
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_main_session_launch_uses_a_blocking_hook(self) -> None:
        config = json.loads(
            (GUARD.parent.parent / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            any(
                "Agent" in entry.get("matcher", "")
                for entry in config["hooks"]["PreToolUse"]
            )
        )
        for event in ("PreToolUse", "UserPromptSubmit"):
            for entry in config["hooks"][event]:
                for hook in entry["hooks"]:
                    self.assertEqual(hook.get("onFailure"), "block")
        for entry in config["hooks"]["PreToolUse"]:
            self.assertIn("NotebookEdit", entry["matcher"])
        payload = {
            **MAIN_LAUNCH,
            "cwd": str(self.project),
            "tool_input": {
                "subagent_type": "task-flow:implementer",
                "prompt": f"Task directory: {self.task}\n",
            },
        }
        result = subprocess.run(
            [sys.executable, str(GUARD.with_name("phase_gate.py")), "hook"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("authorization", result.stderr)


if __name__ == "__main__":
    unittest.main()
