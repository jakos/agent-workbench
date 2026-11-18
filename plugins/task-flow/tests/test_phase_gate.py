# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Offline workflow gates: temporary task artifacts, no models or real sessions."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import approval_witness as witness
import epics
import phase_gate as gate

PLAN = """# Plan: Test
## Goal
Exercise gates.
## Context
Temporary fixtures.
## Profiles
python: src/a.py
## Steps
1. Change src/a.py (python).
## Acceptance criteria
AC1: Behaviour works. Verify: test_a
## Verification commands
```
python -V
```
## Project rules
None.
## Out of scope
Other changes.
## Risks / open questions
plan-risk: low
"""

# Documented payload shapes, not recordings of runtime firing.
MAIN_LAUNCH = {
    "hook_event_name": "PreToolUse",
    "tool_name": "Agent",
    "tool_input": {
        "subagent_type": "task-flow:implementer",
        "description": "Implement",
        "prompt": "",
    },
    "session_id": "fixture-session",
}
PROMPT = {
    "hook_event_name": "UserPromptSubmit",
    "session_id": "fixture-session",
    "prompt": "/task-flow:task-run t1",
}

UNRELATED_SCRIPT_COMMANDS = (
    "uv run --script plugins/task-flow/tests/test_gates.py",
    "uv run --script plugins/task-flow/tests/test_implement.py",
    "uv run --script plugins/task-flow/tests/test_task_record.py",
    "uv run --script plugins/task-flow/tests/test_prune_task.py",
    "cat plugins/task-flow/scripts/implement.py",
    "git log -- gates.py",
    "echo plugins/task-flow/scripts/task_record.py render",
)


class GateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.home = self.root / "home"
        self.home.mkdir()
        environment = patch.dict(
            os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.task = self.project / ".work" / "t1"
        self.task.mkdir(parents=True)
        self.config = self.project / ".claude" / "task-flow.policy.toml"
        self.config.parent.mkdir()
        self.config.write_text('preset = "supervised"\n', encoding="utf-8")
        self.write("plan.md", PLAN)
        self.set_state("planned", 0)

    def write(self, name: str, text: str) -> None:
        (self.task / name).write_text(text, encoding="utf-8", newline="\n")

    def set_state(self, phase: str, number: int) -> None:
        self.write(
            "status.md",
            f"""# Status: t1
- phase: {phase}
- round: {number}
- base: main
- branch: task/t1
- worktree: none
- plan-approved: yes (user, 2025-12-14)
- last-verdict: none
## History
- created
## Log
fixture text that must survive
""",
        )

    def call(self, action: str, **kwargs: object) -> dict:
        return gate.decide(self.task, action, home=self.home, **kwargs)

    def approve(self) -> None:
        self.user_action("approve")

    def user_action(self, action: str, ceiling: int = 0) -> dict:
        gate.authorization_request(
            self.task,
            action,
            ceiling,
            session="fixture-session",
            home=self.home,
        )
        return self.call(action, source="user", ceiling=ceiling)

    def evidence(
        self, number: int, result: str = "PASS", detail: str = "ERROR: test_one"
    ) -> None:
        failure = (
            "none" if result == "PASS" else f"### 1. `python -V`\n```\n{detail}\n```"
        )
        code = "0" if result == "PASS" else "1"
        self.write(
            f"checks-{number}.md",
            f"# Checks: round {number}\n| # | Command | Result | Exit | Duration |\n| --- | --- | --- | --- | --- |\n| 1 | `python -V` | {result} | {code} | 1s |\n## Failures\n{failure}\n## Not run\nnone\n",
        )

    def review(
        self,
        number: int,
        verdict: str = "APPROVED",
        failed_ac: bool = False,
        prior: str = "none",
    ) -> None:
        result = "FAIL" if failed_ac else "PASS"
        fixes = "none" if verdict == "APPROVED" else "1. Fix behaviour."
        self.write(
            f"review-{number}.md",
            f"""# Review: round {number}
## Acceptance criteria
| AC | Result | Evidence |
| --- | --- | --- |
| AC1 | {result} | test_a |
## Pre-existing failures
{prior}
## Findings
none
## Scope
none
## Verdict: {verdict}
## Fix list
{fixes}
""",
        )

    def start(self) -> None:
        self.approve()
        self.call("transition", target="baseline")
        self.evidence(0)
        self.call("transition", target="implementing", number=1)


class WitnessStorageTest(GateFixture):
    def test_strict_storage_and_single_use(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        token = gate.digest(PLAN.encode())
        identity = gate.attempt(*gate.status(self.task), self.task)
        witness.issue(
            self.task,
            "approve",
            token,
            identity,
            0,
            channel="user-prompt",
            session="session",
            now=now,
        )
        path = witness.location(self.task, "approve")
        original = path.read_bytes()
        value = json.loads(original)
        for delta in (-1, 301):
            with self.subTest(delta=delta), self.assertRaises(witness.Refusal):
                witness.consume(
                    self.task,
                    "approve",
                    token,
                    identity,
                    0,
                    now=now + timedelta(seconds=delta),
                )
            self.assertEqual(path.read_bytes(), original)
        for key, changed in (
            ("repository", "0" * 64),
            ("task", "other"),
            ("plan", "0" * 64),
            ("attempt", "0" * 64),
            ("action", "grant"),
            ("ceiling", 4),
            ("session", None),
            ("session", ""),
            ("session", "   "),
            ("nonce", "bad"),
            ("extra", "field"),
        ):
            with self.subTest(key=key), self.assertRaises(witness.Refusal):
                path.write_text(json.dumps({**value, key: changed}), encoding="utf-8")
                witness.consume(self.task, "approve", token, identity, 0, now=now)
        path.write_bytes(original)
        reference = witness.consume(self.task, "approve", token, identity, 0, now=now)
        self.assertEqual(reference["channel"], "user-prompt")
        self.assertNotIn(value["nonce"], json.dumps(reference))
        with self.assertRaises(witness.Refusal):
            witness.consume(self.task, "approve", token, identity, 0, now=now)
        path.write_bytes(original)
        with self.assertRaisesRegex(witness.Refusal, "already consumed"):
            witness.consume(self.task, "approve", token, identity, 0, now=now)

    def test_interrupted_storage_does_not_publish(self) -> None:
        with (
            patch.object(witness.os, "replace", side_effect=OSError("interrupted")),
            self.assertRaises(OSError),
        ):
            witness.issue(
                self.task,
                "approve",
                "0" * 64,
                "1" * 64,
                0,
                channel="user-prompt",
                session="fixture-session",
            )
        self.assertFalse(witness.location(self.task, "approve").exists())
        self.assertEqual(
            list((self.task.parent / witness.DIRECTORY).glob(".witness-*")), []
        )

    def test_malformed_and_duplicate_storage_refuses(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        witness.issue(
            self.task,
            "approve",
            "0" * 64,
            "1" * 64,
            0,
            channel="user-prompt",
            session="fixture-session",
            now=now,
        )
        path = witness.location(self.task, "approve")
        original = path.read_text(encoding="utf-8").strip()
        for raw in (
            "{",
            "[]",
            original[:-1] + ', "ceiling": 0}',
            original.replace('"user-prompt"', '"unknown"'),
        ):
            path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(witness.Refusal):
                witness.consume(self.task, "approve", "0" * 64, "1" * 64, 0, now=now)

    def test_storage_lock_is_reserved_and_prevents_concurrent_consumption(self) -> None:
        witness.issue(
            self.task,
            "approve",
            "0" * 64,
            "1" * 64,
            0,
            channel="user-prompt",
            session="fixture-session",
        )
        with witness.locked(self.task), self.assertRaises(witness.Refusal):
            witness.consume(self.task, "approve", "0" * 64, "1" * 64, 0)
        self.assertTrue(
            (witness.location(self.task, "approve").parent / ".lock-t1").is_file()
        )
        witness.consume(self.task, "approve", "0" * 64, "1" * 64, 0)


class WitnessGateTest(GateFixture):
    def prompt(self, command: str, **changes: object) -> str | None:
        return gate.hook(
            {**PROMPT, "cwd": str(self.project), "prompt": command, **changes},
            home=self.home,
        )

    def test_prompt_approval_grant_action_ceiling_and_replay(self) -> None:
        self.assertIsNone(self.prompt("/task-flow:task-approve t1"))
        with self.assertRaises(gate.Refusal):
            self.call("grant", source="user", ceiling=4)
        approved = self.call("approve", source="user")
        self.assertEqual(approved["authorization"]["channel"], "user-prompt")
        self.assertTrue(
            gate.status(self.task)[1]["plan-approved"].startswith("yes (user,")
        )
        with self.assertRaises(gate.Refusal):
            self.call("approve", source="user")
        self.assertIsNone(self.prompt("/task-flow:task-grant t1 4"))
        with self.assertRaises(gate.Refusal):
            self.call("approve", source="user")
        with self.assertRaises(gate.Refusal):
            self.call("grant", source="user", ceiling=5)
        granted = self.call("grant", source="user", ceiling=4)
        self.assertEqual(granted["authorization"]["channel"], "user-prompt")
        stored = list((self.task.parent / witness.DIRECTORY).glob("consumed-*"))
        audit = (self.task / "gates.jsonl").read_text(encoding="utf-8")
        for path in stored:
            self.assertNotIn(json.loads(path.read_bytes())["nonce"], audit)
        self.assertNotIn("/task-flow:", audit)

    def test_unrecognized_and_non_user_prompts_never_issue(self) -> None:
        for command in (
            "approve t1",
            "please /task-flow:task-approve t1",
            "`/task-flow:task-approve t1`",
            "/task-flow:task-approve t1 t2",
            "/task-flow:task-grant t1",
            "/task-flow:task-approve t1 4",
            "/task-flow:task-grant t1 0",
            "/task-flow:task-grant t1 -1",
            "/task-flow:task-grant t1 4 extra",
            " /task-flow:task-approve t1",
        ):
            with self.subTest(command=command):
                self.prompt(command)
                self.assertFalse(witness.location(self.task, "approve").exists())
                self.assertFalse(witness.location(self.task, "grant").exists())
        for changes in (
            {"session_id": ""},
            {"session_id": None},
            {"agent_type": "task-flow:implementer"},
            {"tool_name": "Skill"},
            {"tool_input": {"skill": "task-flow:task-approve"}},
        ):
            with self.subTest(changes=changes):
                self.assertIsNotNone(
                    self.prompt("/task-flow:task-approve t1", **changes)
                )
                self.assertFalse(witness.location(self.task, "approve").exists())
        self.assertIsNotNone(self.prompt("/task-flow:task-approve absent"))
        self.write("plan.md", "invalid plan")
        self.assertIsNotNone(self.prompt("/task-flow:task-approve t1"))

    def test_shortcut_changed_plan_attempt_and_expiry_refuse(self) -> None:
        for action, ceiling in (("approve", 0), ("grant", 4)):
            with self.subTest(action=action), self.assertRaises(gate.Refusal):
                self.call(action, source="user", ceiling=ceiling)
        self.prompt("/task-flow:task-approve t1")
        self.write("plan.md", PLAN.replace("Exercise gates.", "Changed scope."))
        with self.assertRaises(gate.Refusal):
            self.call("approve", source="user")
        self.write("plan.md", PLAN)
        self.prompt("/task-flow:task-approve t1")
        text, _ = gate.status(self.task)
        self.write("status.md", text.replace("base: main", "base: other"))
        with self.assertRaises(gate.Refusal):
            self.call("approve", source="user")
        self.write("status.md", text)
        for age in (-1, 301):
            self.prompt("/task-flow:task-approve t1")
            path = witness.location(self.task, "approve")
            data = json.loads(path.read_bytes())
            data["issued"] = (datetime.now(UTC) - timedelta(seconds=age)).isoformat()
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.subTest(age=age), self.assertRaises(gate.Refusal):
                self.call("approve", source="user")

    def test_concurrent_consumers_only_one_authorizes(self) -> None:
        self.prompt("/task-flow:task-approve t1")

        def consume(_: int) -> bool:
            try:
                self.call("approve", source="user")
                return True
            except gate.Refusal:
                return False

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(consume, range(12))), 1)
        self.assertEqual(sum(e["allowed"] for e in gate.log_entries(self.task)), 1)
        # Independent processes exercise the persistent OS lock and atomic burn,
        # beyond the same-process contention above.
        self.prompt("/task-flow:task-approve t1")
        consumers = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(gate.__file__)),
                    "approve",
                    "--source",
                    "user",
                    "--task-dir",
                    str(self.task),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.project,
            )
            for _ in range(4)
        ]
        try:
            for consumer in consumers:
                consumer.communicate(timeout=30)
            self.assertEqual(sum(p.returncode == 0 for p in consumers), 1)
            self.assertTrue(all(p.returncode in (0, 2) for p in consumers))
            self.assertEqual(sum(e["allowed"] for e in gate.log_entries(self.task)), 2)
        finally:
            for consumer in consumers:
                if consumer.poll() is None:
                    consumer.kill()
                consumer.communicate()

    def test_persistence_failures_burn_evidence_and_preserve_status(self) -> None:
        before = (self.task / "status.md").read_bytes()
        for operation in ("append", "publish"):
            self.prompt("/task-flow:task-approve t1")
            with (
                patch.object(gate, operation, side_effect=OSError("interrupted")),
                self.assertRaises(gate.Refusal),
            ):
                self.call("approve", source="user")
            self.assertEqual((self.task / "status.md").read_bytes(), before)
            with self.assertRaises(gate.Refusal):
                self.call("approve", source="user")
        self.prompt("/task-flow:task-approve t1")
        with (
            patch.object(witness.os, "replace", side_effect=OSError("disk full")),
            self.assertRaises(gate.Refusal),
        ):
            self.call("approve", source="user")
        self.assertFalse(gate.log_entries(self.task)[-1]["allowed"])
        self.assertEqual((self.task / "status.md").read_bytes(), before)
        self.prompt("/task-flow:task-approve t1")
        self.call("approve", source="user")

    def test_interrupted_audit_requires_recovery_and_fresh_authorization(self) -> None:
        self.prompt("/task-flow:task-approve t1")
        before = (self.task / "status.md").read_bytes()

        def interrupted(task: Path, event: dict) -> None:
            with (task / "gates.jsonl").open("ab") as handle:
                handle.write(json.dumps(event).encode()[:40])
            raise OSError("interrupted audit append")

        with (
            patch.object(gate, "append", side_effect=interrupted),
            self.assertRaises(gate.Refusal),
        ):
            self.call("approve", source="user")
        self.assertEqual((self.task / "status.md").read_bytes(), before)
        self.assertFalse(witness.location(self.task, "approve").exists())
        self.assertIsNotNone(self.prompt("/task-flow:task-approve t1"))
        gate.recover(self.task, human=True)
        with self.assertRaises(gate.Refusal):
            self.call("approve", source="user")
        self.prompt("/task-flow:task-approve t1")
        self.call("approve", source="user")

    def test_issuance_failure_and_invalid_phase_never_authorize(self) -> None:
        with patch.object(witness, "issue", side_effect=OSError("disk full")):
            self.assertIsNotNone(self.prompt("/task-flow:task-approve t1"))
        with self.assertRaises(gate.Refusal):
            self.call("approve", source="user")
        self.set_state("implementing", 1)
        self.assertIsNotNone(self.prompt("/task-flow:task-approve t1"))
        self.assertFalse(witness.location(self.task, "approve").exists())
        self.assertIsNotNone(self.prompt("/task-flow:task-grant t1 3"))
        self.set_state("planned", 0)
        self.write(
            "plan.md", PLAN.replace("plan-risk: low", "BLOCKED: yes\nUnresolved.")
        )
        for command in ("/task-flow:task-approve t1", "/task-flow:task-grant t1 4"):
            self.assertIsNotNone(self.prompt(command))
        self.assertFalse(witness.location(self.task, "approve").exists())
        self.assertFalse(witness.location(self.task, "grant").exists())

    def test_tools_allow_consumers_and_deny_issuance_without_policy(self) -> None:
        self.config.unlink()
        self.assertIsNone(self.prompt("/task-flow:task-approve t1"))
        self.assertFalse(witness.location(self.task, "approve").exists())
        for tool in ("Bash", "PowerShell"):
            payload = {"cwd": str(self.project), "tool_name": tool}
            for action in ("approve", "grant"):
                command = f"python plugins/task-flow/scripts/phase_gate.py {action} --source user --task-dir {self.task}"
                self.assertIsNone(
                    gate.hook(
                        {**payload, "tool_input": {"command": command}}, home=self.home
                    )
                )
            for command in (
                "python phase_gate.py hook",
                'python "phase_gate.py" "hook"',
                "py 'phase_gate.py' 'hook'",
                'python phase_gate.py h"oo"k',
                'python -m "phase_gate" "hook"',
            ):
                self.assertIsNotNone(
                    gate.hook(
                        {**payload, "tool_input": {"command": command}}, home=self.home
                    )
                )
        self.assertFalse(witness.location(self.task, "approve").exists())
        self.assertFalse(witness.location(self.task, "grant").exists())

    def test_relative_witness_mutations_refuse_without_policy(self) -> None:
        self.config.unlink()
        (self.project / ".git").mkdir()
        reserved = self.project / ".work" / ".approval-witnesses"
        reserved.mkdir()
        for agent in (None, "task-flow:implementer", "task-flow:reviewer"):
            payload = {"cwd": str(reserved), "agent_type": agent}
            for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                key = "notebook_path" if tool == "NotebookEdit" else "file_path"
                with self.subTest(agent=agent, tool=tool):
                    self.assertIsNotNone(
                        gate.hook(
                            {
                                **payload,
                                "tool_name": tool,
                                "tool_input": {key: "t1-approve.json"},
                            },
                            home=self.home,
                        )
                    )
            for tool in ("Bash", "PowerShell"):
                for command in (
                    "echo forged > t1-approve.json",
                    "Remove-Item t1-approve.json",
                ):
                    with self.subTest(agent=agent, tool=tool, command=command):
                        self.assertIsNotNone(
                            gate.hook(
                                {
                                    **payload,
                                    "tool_name": tool,
                                    "tool_input": {"command": command},
                                },
                                home=self.home,
                            )
                        )

    def test_removed_terminal_options_refuse_without_issuance(self) -> None:
        audit = self.task / "gates.jsonl"
        audit.write_bytes(b"")
        before = ((self.task / "status.md").read_bytes(), audit.read_bytes())
        for action, ceiling in (("approve", 0), ("grant", 4)):
            argv = [
                action,
                "--task-dir",
                str(self.task),
                "--source",
                "user",
                "--ceiling",
                str(ceiling),
            ]
            for length in range(1, len("terminal") + 1):
                option = "--" + "terminal"[:length]
                with (
                    self.subTest(action=action, option=option),
                    patch.object(gate, "human_confirmation") as confirm,
                    patch.object(gate, "authorization_request") as issue,
                    patch("builtins.input") as challenge,
                    patch.object(gate.sys, "stderr"),
                    self.assertRaises(SystemExit) as error,
                ):
                    gate.main([*argv, option])
                self.assertEqual(error.exception.code, 2)
                confirm.assert_not_called()
                issue.assert_not_called()
                challenge.assert_not_called()
            # Execute both real entry points from an isolated project. -Bm combines
            # the interpreter flags that bypassed the former text matcher.
            for invocation in ([str(Path(gate.__file__))], ["-Bm", "phase_gate"]):
                with self.subTest(action=action, invocation=invocation):
                    result = subprocess.run(
                        [sys.executable, *invocation, *argv, "--terminal"],
                        input="forged confirmation\n",
                        text=True,
                        capture_output=True,
                        cwd=self.project,
                        env={
                            **os.environ,
                            "PYTHONPATH": str(Path(gate.__file__).parent),
                        },
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("unrecognized arguments: --terminal", result.stderr)
                    self.assertEqual(result.stdout, "")
            self.assertFalse(witness.location(self.task, action).exists())
        self.assertFalse((self.task.parent / witness.DIRECTORY).exists())
        self.assertEqual(
            ((self.task / "status.md").read_bytes(), audit.read_bytes()), before
        )

    def test_bare_consumers_require_prompt_evidence(self) -> None:
        for action, ceiling in (("approve", 0), ("grant", 4)):
            argv = [
                action,
                "--task-dir",
                str(self.task),
                "--source",
                "user",
                "--ceiling",
                str(ceiling),
            ]
            before = (self.task / "status.md").read_bytes()
            for interactive in (False, True):
                with (
                    self.subTest(action=action, interactive=interactive),
                    patch.object(gate.sys.stdin, "isatty", return_value=interactive),
                    patch.object(gate.sys.stdout, "isatty", return_value=interactive),
                    patch.object(gate, "human_confirmation") as confirm,
                    patch.object(gate, "authorization_request") as issue,
                    patch("builtins.input") as challenge,
                    patch.object(gate.sys, "stderr"),
                ):
                    self.assertEqual(gate.main(argv), 2)
                    confirm.assert_not_called()
                    issue.assert_not_called()
                    challenge.assert_not_called()
                self.assertEqual((self.task / "status.md").read_bytes(), before)
                self.assertFalse(witness.location(self.task, action).exists())
            command = f"/task-flow:task-{action} t1" + (
                f" {ceiling}" if ceiling else ""
            )
            self.assertIsNone(self.prompt(command))
            with patch.object(gate.sys, "stdout"):
                self.assertEqual(gate.main(argv), 0)
            self.assertEqual(
                gate.log_entries(self.task)[-1]["authorization"]["channel"],
                "user-prompt",
            )

    def test_terminal_witnesses_refuse_but_audit_remains_readable(self) -> None:
        token = gate.digest(PLAN.encode())
        identity = gate.attempt(*gate.status(self.task), self.task)
        before = (self.task / "status.md").read_bytes()
        for action, ceiling in (("approve", 0), ("grant", 4)):
            for session in (None, "session"):
                with (
                    self.subTest(action=action, session=session),
                    self.assertRaises(witness.Refusal),
                ):
                    witness.issue(
                        self.task,
                        action,
                        token,
                        identity,
                        ceiling,
                        channel="terminal",
                        session=session,
                    )
                self.assertFalse(witness.location(self.task, action).exists())
            for session in (None, "", "   "):
                with self.subTest(session=session), self.assertRaises(witness.Refusal):
                    witness.issue(
                        self.task,
                        action,
                        token,
                        identity,
                        ceiling,
                        channel="user-prompt",
                        session=session,
                    )
            self.assertIsNone(
                self.prompt(
                    f"/task-flow:task-{action} t1" + (f" {ceiling}" if ceiling else "")
                )
            )
            path = witness.location(self.task, action)
            value = json.loads(path.read_bytes())
            value.update(channel="terminal", session=None)
            path.write_text(json.dumps(value), encoding="utf-8")
            legacy_bytes = path.read_bytes()
            with self.assertRaises(gate.Refusal):
                self.call(action, source="user", ceiling=ceiling)
            self.assertEqual(path.read_bytes(), legacy_bytes)
            self.assertEqual((self.task / "status.md").read_bytes(), before)
        self.approve()
        entries = gate.log_entries(self.task)
        entries[-1]["authorization"]["channel"] = "terminal"
        audit = self.task / "gates.jsonl"
        audit.write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
        )
        historical = audit.read_bytes()
        self.assertEqual(gate.log_entries(self.task), entries)
        self.assertEqual(audit.read_bytes(), historical)
        self.call("transition", target="baseline")
        self.assertTrue(audit.read_bytes().startswith(historical))

    def test_recovery_cli_requires_interactive_confirmation_without_issuance(
        self,
    ) -> None:
        audit = self.task / "gates.jsonl"
        audit.write_bytes(b"")
        original = audit.read_bytes()
        before = (self.task / "status.md").read_bytes()
        argv = ["recover", "--source", "user", "--task-dir", str(self.task)]
        token = gate.digest(PLAN.encode())
        identity = gate.attempt(*gate.status(self.task), self.task)
        for stdin, stdout, answer in (
            (False, True, ""),
            (True, False, ""),
            (True, True, "yes"),
            (True, True, f"recover t1 {token} {identity} 0"),
        ):
            accepted = stdin and stdout and answer.startswith("recover ")
            with (
                self.subTest(stdin=stdin, stdout=stdout, answer=answer),
                patch.object(gate.sys.stdin, "isatty", return_value=stdin),
                patch.object(gate.sys.stdout, "isatty", return_value=stdout),
                patch("builtins.input", return_value=answer),
                patch("builtins.print"),
                patch.object(gate, "authorization_request") as issue,
                patch.object(gate, "recover") as recovery,
            ):
                self.assertEqual(gate.main(argv), 0 if accepted else 2)
                issue.assert_not_called()
            self.assertEqual((self.task / "status.md").read_bytes(), before)
            self.assertFalse(witness.location(self.task, "approve").exists())
            self.assertFalse(witness.location(self.task, "grant").exists())
            if not accepted:
                self.assertEqual(audit.read_bytes(), original)
            self.assertEqual(recovery.called, accepted)
        self.assertFalse((self.task.parent / witness.DIRECTORY).exists())
        self.assertIn(
            "audit recovery",
            gate.hook(
                {
                    "cwd": str(self.project),
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": f"python phase_gate.py recover --source user --task-dir {self.task}"
                    },
                },
                home=self.home,
            )
            or "",
        )

    def test_audit_reference_strictness_and_legacy_readability(self) -> None:
        self.approve()
        event = gate.log_entries(self.task)[0]
        legacy = {k: v for k, v in event.items() if k != "authorization"}
        self.assertEqual(
            gate.parse_log((json.dumps(legacy) + "\n").encode(), "t1"), [legacy]
        )
        for changed in (
            {"channel": "fake", "reference": "0" * 64},
            {"channel": "terminal", "reference": "raw nonce"},
            {},
        ):
            with self.subTest(changed=changed), self.assertRaises(gate.Refusal):
                gate.parse_log(
                    (json.dumps({**event, "authorization": changed}) + "\n").encode(),
                    "t1",
                )
        with self.assertRaisesRegex(gate.Refusal, "authorization reference"):
            gate.parse_log(((json.dumps(event) + "\n") * 2).encode(), "t1")


class PhaseGateTest(GateFixture):
    def test_legacy_preserves_old_behaviour(self) -> None:
        self.config.unlink()
        self.write("status.md", "not a valid new status")
        self.assertEqual(self.call("implement")["reason"], "legacy")
        self.assertFalse((self.task / "gates.jsonl").exists())

    def test_initialize_and_plan_confirmation_race(self) -> None:
        (self.task / "status.md").unlink()
        gate.initialize(self.task, "main", home=self.home)
        gate.initialize(self.task, "main", home=self.home)
        self.call("transition", target="planned")
        gate.authorization_request(
            self.task, "approve", 0, session="fixture-session", home=self.home
        )
        self.write("plan.md", PLAN.replace("Exercise gates.", "Changed scope."))
        with self.assertRaisesRegex(gate.Refusal, "does not match"):
            self.call("approve", source="user")
        self.write("plan.md", PLAN)
        self.approve()
        self.call("note", message="round 1 implementer: fixture.py")
        self.assertIn(
            "fixture.py", (self.task / "status.md").read_text(encoding="utf-8")
        )

    def test_full_flow_and_resumes(self) -> None:
        self.start()
        for _ in range(2):
            self.call("implement", number=1)
        self.call("transition", target="checking")
        self.call("check", number=1)
        self.evidence(1)
        self.call("transition", target="reviewing")
        self.review(1)
        self.call("transition", target="review-approved")
        self.call("finish")
        self.call("transition", target="done")
        before = (self.task / "status.md").read_bytes()
        self.call("transition", target="done")
        self.assertEqual((self.task / "status.md").read_bytes(), before)
        self.assertIn(b"fixture text that must survive", before)
        self.assertEqual(gate.status(self.task)[1]["phase"], "done")

    def test_matrix_illegal_jumps_every_declared_phase(self) -> None:
        self.approve()
        for phase in sorted(gate.PHASES):
            for target in sorted(gate.PHASES - gate.EDGES[phase] - {phase}):
                with self.subTest(phase=phase, target=target):
                    self.set_state(
                        phase,
                        0 if phase in ("new", "planned", "approved", "baseline") else 1,
                    )
                    before = (self.task / "status.md").read_bytes()
                    with self.assertRaises(gate.Refusal):
                        self.call("transition", target=target)
                    self.assertEqual((self.task / "status.md").read_bytes(), before)

    def test_status_malformed_duplicate_missing_identity(self) -> None:
        original = (self.task / "status.md").read_text(encoding="utf-8")
        for text in (
            original + "\n",  # valid control, tested separately below
            original.replace("- round: 0\n", ""),
            original.replace("- round: 0", "- round: 0\n- round: 0"),
            original.replace("- phase: planned", "- phase: surprise"),
            original.replace("- round: 0", "- round: -1"),
            original.replace("# Status: t1", "# Status: other"),
            original.replace("- worktree: none", "- worktree: ../other"),
        ):
            if text == original + "\n":
                continue
            with self.subTest(text=text):
                self.write("status.md", text)
                with self.assertRaises(gate.Refusal):
                    self.call("approve", source="user")
                self.assertEqual(
                    (self.task / "status.md").read_text(encoding="utf-8"), text
                )
        self.assertTrue(all(not e["allowed"] for e in gate.log_entries(self.task)))

    def test_fabricated_status_auto_and_stale_authorization(self) -> None:
        self.set_state("implementing", 1)
        self.evidence(0)
        with self.assertRaisesRegex(gate.Refusal, "authorization"):
            self.call("implement")
        self.set_state("planned", 0)
        for source in ("user", "--auto", "policy"):
            with self.assertRaises(gate.Refusal):
                self.call("approve", source=source)
        self.start()
        self.write("plan.md", PLAN.replace("Exercise gates.", "Changed scope."))
        with self.assertRaisesRegex(gate.Refusal, "authorization"):
            self.call("implement")

    def test_policy_approval_rechecked(self) -> None:
        self.config.write_text('plan_approval = "auto"\n', encoding="utf-8")
        self.call("approve")
        self.evidence(0)
        self.set_state("implementing", 1)
        self.call("implement")
        self.config.write_text('plan_approval = "ask"\n', encoding="utf-8")
        with self.assertRaisesRegex(gate.Refusal, "no longer"):
            self.call("implement")

    def test_missing_and_malformed_baseline(self) -> None:
        self.approve()
        self.set_state("baseline", 0)
        for content in (None, "# Checks: round 0\n", "garbage"):
            if content is not None:
                self.write("checks-0.md", content)
            with self.assertRaises(gate.Refusal):
                self.call("transition", target="implementing", number=1)

    def test_limits_finite_grants_and_attempt_binding(self) -> None:
        self.config.write_text("max_rounds = 1\n", encoding="utf-8")
        self.start()
        self.evidence(1)
        self.review(1, "CHANGES_REQUESTED", failed_ac=True)
        self.set_state("reviewing", 1)
        with self.assertRaisesRegex(gate.Refusal, "round cap"):
            self.call("transition", target="implementing", number=2)
        for args in (
            {"source": "policy", "ceiling": 4},
            {"source": "user", "ceiling": 4},
            {"source": "user", "ceiling": 0},
        ):
            with self.assertRaises(gate.Refusal):
                self.call("grant", **args)
        self.user_action("grant", ceiling=4)
        self.call("transition", target="implementing", number=2)
        self.call("implement", number=2)
        self.set_state("implementing", 5)
        with self.assertRaisesRegex(gate.Refusal, "round cap"):
            self.call("implement", number=5)
        self.set_state("implementing", 2)
        text = (self.task / "status.md").read_text(encoding="utf-8")
        self.write(
            "status.md", text.replace("- created", "- created\n- retry authorized")
        )
        self.call("implement")  # An invocation retry is the same attempt.
        archive = (
            self.project / ".work" / "_epics" / "e1" / "attempts" / "t1" / "attempt-1"
        )
        archive.mkdir(parents=True)
        (archive / "spend.md").write_text("archived evidence", encoding="utf-8")
        self.write(
            "status.md",
            text.replace(
                "- created",
                "- created\n- round reset to 0 for attempt 1 of epic e1 — authorized by the user",
            ),
        )
        with self.assertRaisesRegex(gate.Refusal, "authorization"):
            self.call("implement")

    def test_prompt_grant_cannot_cross_attempts(self) -> None:
        self.start()
        gate.authorization_request(
            self.task, "grant", 4, session="fixture-session", home=self.home
        )
        text, _ = gate.status(self.task)
        self.write("status.md", text.replace("- base: main", "- base: changed-base"))
        with self.assertRaisesRegex(gate.Refusal, "does not match"):
            self.call("grant", source="user", ceiling=4)
        self.assertFalse(gate.log_entries(self.task)[-1]["allowed"])

    def test_no_progress_nonempty_only_and_baseline_exclusion(self) -> None:
        commands = ["python -V"]
        self.evidence(0, "FAIL")
        for number in (1, 2):
            self.evidence(number, "FAIL")
            self.review(
                number,
                "CHANGES_REQUESTED",
                prior="python -V matches checks-0: ERROR: test_one",
            )
        self.assertIsNone(gate.stopped(self.task, 2, PLAN, commands, 3))
        for number in (1, 2):
            self.evidence(number, "FAIL", "ERROR: test_new")
            self.review(number, "CHANGES_REQUESTED")
        self.assertEqual(gate.stopped(self.task, 2, PLAN, commands, 3), "no progress")
        self.evidence(2)
        self.review(2, "CHANGES_REQUESTED", failed_ac=True)
        self.assertIsNone(gate.stopped(self.task, 2, PLAN, commands, 3))
        self.evidence(1)
        self.review(1, "CHANGES_REQUESTED", failed_ac=True)
        self.assertEqual(gate.stopped(self.task, 2, PLAN, commands, 3), "no progress")

    def test_finish_requires_complete_review_and_checks(self) -> None:
        self.start()
        self.set_state("review-approved", 1)
        for review_text in (None, "# Review: round 1\n## Verdict: APPROVED\n"):
            if review_text:
                self.write("review-1.md", review_text)
            with self.assertRaises(gate.Refusal):
                self.call("finish")
        self.evidence(1, "FAIL")
        self.review(1)
        with self.assertRaises(gate.Refusal):
            self.call("finish")
        self.evidence(1)
        self.call("finish")

    def test_append_only_repeated_write_failure_and_recovery(self) -> None:
        self.approve()
        path = self.task / "gates.jsonl"
        previous = path.read_bytes()
        self.approve()
        self.assertTrue(path.read_bytes().startswith(previous))
        before = (self.task / "status.md").read_bytes()
        with (
            patch.object(gate, "append", side_effect=OSError("fixture")),
            self.assertRaises(gate.Refusal),
        ):
            self.call("transition", target="baseline")
        self.assertEqual(before, (self.task / "status.md").read_bytes())
        with path.open("ab") as handle:
            handle.write(b'{"incomplete":')
        interrupted = path.read_bytes()
        with self.assertRaises(gate.Refusal):
            self.call("transition", target="baseline")
        with self.assertRaises(gate.Refusal):
            gate.recover(self.task, human=False)
        gate.recover(self.task, human=True)
        self.assertTrue(path.read_bytes().startswith(interrupted))
        self.call("transition", target="baseline")
        self.assertEqual(gate.log_entries(self.task)[-2]["action"], "recover")

    def test_decision_records_authority_and_actual_denied_state(self) -> None:
        self.approve()
        before = (self.task / "status.md").read_bytes()
        with self.assertRaisesRegex(gate.Refusal, "worktree metadata"):
            self.call("transition", target="baseline", worktree="other")
        event = gate.log_entries(self.task)[-1]
        self.assertFalse(event["allowed"])
        self.assertEqual(event["before"], event["after"])
        self.assertEqual(event["source"], "user")
        self.assertEqual((self.task / "status.md").read_bytes(), before)
        self.call("transition", target="baseline")
        self.assertEqual(gate.log_entries(self.task)[-1]["source"], "user")

    def test_status_publication_retries_only_windows_sharing_violations(self) -> None:
        path = self.task / "status.md"
        before = path.read_bytes()
        sharing = OSError("temporary sharing violation")
        sharing.winerror = 32
        for error in (sharing, PermissionError("access denied")):
            replace = unittest.mock.Mock(side_effect=error)
            pause = unittest.mock.Mock()
            with self.assertRaises(OSError):
                gate.publish(path, "replacement", replace=replace, pause=pause)
            self.assertEqual(replace.call_count, 5 if error is sharing else 1)
            self.assertEqual(pause.call_count, 4 if error is sharing else 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(self.task.glob(".gate-*")), [])
        replace = unittest.mock.Mock(side_effect=[sharing, None])

        def replace_after_sharing(source: str, destination: Path) -> None:
            replace(source, destination)
            os.replace(source, destination)

        gate.publish(path, "replacement", replace=replace_after_sharing, pause=pause)
        self.assertEqual(replace.call_count, 2)
        self.assertEqual(path.read_text(encoding="utf-8"), "replacement")

    def test_malformed_log_and_concurrent_writers(self) -> None:
        self.approve()
        path = self.task / "gates.jsonl"
        prefix = path.read_bytes()

        def write_decision(_: int) -> None:
            for _ in range(100):
                try:
                    self.call("dispatch")
                    return
                except gate.Refusal as exc:
                    if "unavailable" not in str(exc):
                        raise
                    time.sleep(0.005)
            self.fail("writer could not obtain lock")

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write_decision, range(12)))
        self.assertEqual(len(gate.log_entries(self.task)), 13)
        self.assertTrue(path.read_bytes().startswith(prefix))
        with path.open("ab") as handle:
            handle.write(b"{}\n")
        before = path.read_bytes()
        with self.assertRaises(gate.Refusal):
            self.call("dispatch")
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_requests_keep_parseable_audit_and_duplicate_keys_refuse(
        self,
    ) -> None:
        self.approve()
        for args in (
            {"source": "model"},
            {"source": "user", "ceiling": -1},
        ):
            with self.assertRaises(gate.Refusal):
                self.call("grant", **args)
            self.assertFalse(gate.log_entries(self.task)[-1]["allowed"])
        path = self.task / "gates.jsonl"
        event = json.dumps(gate.log_entries(self.task)[0])
        with path.open("ab") as handle:
            handle.write((event[:-1] + ', "allowed": true}\n').encode())
        with self.assertRaisesRegex(gate.Refusal, "malformed gate log"):
            self.call("dispatch")

    def test_malformed_logged_phase_refuses_without_changing_evidence(self) -> None:
        self.approve()
        path = self.task / "gates.jsonl"
        entry = gate.log_entries(self.task)[0]
        entry["after"]["phase"] = []
        path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(gate.Refusal, "invalid logged state"):
            self.call("dispatch")
        self.assertEqual(path.read_bytes(), before)

    def test_checker_launch_is_gated_before_verification(self) -> None:
        self.approve()
        payload = {
            **MAIN_LAUNCH,
            "cwd": str(self.project),
            "tool_input": {
                "subagent_type": "task-flow:checker",
                "prompt": f"Task directory: {self.task}\n",
            },
        }
        self.assertIn("phase", gate.hook(payload, home=self.home))
        self.call("transition", target="baseline")
        self.assertIsNone(gate.hook(payload, home=self.home))
        self.evidence(0)
        self.call("transition", target="implementing", number=1)
        self.assertIn("phase", gate.hook(payload, home=self.home))
        self.call("transition", target="checking")
        self.assertIsNone(gate.hook(payload, home=self.home))

    def test_documented_payloads_context_and_no_prompt_authority(self) -> None:
        payload = json.loads(json.dumps(MAIN_LAUNCH))
        payload["cwd"] = str(self.project)
        self.assertIn("context", gate.hook(payload, home=self.home))
        payload["tool_input"]["prompt"] = f"Task directory: {self.task}\n"
        self.assertIn("authorization", gate.hook(payload, home=self.home))
        prompt = {**PROMPT, "cwd": str(self.project)}
        self.assertIsNone(gate.hook(prompt, home=self.home))
        self.assertFalse(
            any(
                e["allowed"] and e["action"] == "approve"
                for e in gate.log_entries(self.task)
            )
        )
        self.start()
        self.assertIsNone(gate.hook(payload, home=self.home))
        payload["tool_input"]["prompt"] = "Task directory: " + str(
            self.task.with_name("other")
        )
        self.assertIn("ambiguous", gate.hook(payload, home=self.home))
        unrelated = {
            "cwd": str(self.project),
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
        }
        self.assertIsNone(gate.hook(unrelated, home=self.home))

    def test_terminal_stops_grant_resume_and_blocked(self) -> None:
        self.config.write_text("max_rounds = 1\n", encoding="utf-8")
        self.start()
        self.evidence(1)
        self.review(1, "CHANGES_REQUESTED", failed_ac=True)
        self.set_state("reviewing", 1)
        self.call("transition", target="escalated")
        with self.assertRaisesRegex(gate.Refusal, "round cap"):
            self.call("transition", target="implementing", number=2)
        self.user_action("grant", ceiling=3)
        self.call("transition", target="implementing", number=2)
        self.evidence(2)
        self.review(2, "CHANGES_REQUESTED", failed_ac=True)
        self.set_state("reviewing", 2)
        self.call("transition", target="escalated")
        self.user_action("grant", ceiling=4)
        with self.assertRaisesRegex(gate.Refusal, "no progress"):
            self.call("transition", target="implementing", number=3)
        self.set_state("implementing", 2)
        self.call("note", message="BLOCKED: plan is wrong because fixture")
        self.call("transition", target="blocked")
        with self.assertRaises(gate.Refusal):
            self.call("transition", target="implementing", number=3)

    def test_status_mutation_and_context_are_narrow(self) -> None:
        self.start()
        payload = {
            "cwd": str(self.project),
            "session_id": "s1",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(self.task / "status.md"),
                "content": "fabricated",
            },
        }
        self.assertIn("direct status", gate.hook(payload, home=self.home))
        self.assertEqual(
            gate.log_entries(self.task)[-1]["action"], "direct-status-mutation"
        )
        gate.hook({**PROMPT, "cwd": str(self.project)}, home=self.home)
        gate.hook(
            {**PROMPT, "prompt": "unrelated request", "cwd": str(self.project)},
            home=self.home,
        )
        commit = {
            "cwd": str(self.project),
            "session_id": PROMPT["session_id"],
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m unrelated"},
        }
        self.assertIsNone(gate.hook(commit, home=self.home))
        worktree = self.task / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("fixture", encoding="utf-8")
        launch = {
            **MAIN_LAUNCH,
            "cwd": str(worktree),
            "tool_input": {
                "subagent_type": "task-flow:implementer",
                "prompt": f"Task directory: {self.task}\n",
            },
        }
        self.assertIsNone(gate.hook(launch, home=self.home))

    def test_record_finish_hook_checks_review_before_generation(self) -> None:
        self.start()
        payload = {
            "cwd": str(self.project),
            "tool_name": "PowerShell",
            "tool_input": {
                "command": f'uv run --script plugins/task-flow/scripts/task_record.py render --repo "{self.project}" --task-dir "{self.task}"'
            },
        }
        self.assertIn("phase", gate.hook(payload, home=self.home))
        self.assertFalse(gate.log_entries(self.task)[-1]["allowed"])
        payload["tool_input"]["command"] = (
            f'uv run --script "plugins/task-flow/scripts/task_record.py" render --repo "{self.project}" --task-dir "{self.task}"'
        )
        self.assertIn("phase", gate.hook(payload, home=self.home))
        self.evidence(1)
        self.review(1)
        self.set_state("review-approved", 1)
        self.assertIsNone(gate.hook(payload, home=self.home))
        self.assertEqual(gate.log_entries(self.task)[-1]["action"], "finish")

    def test_script_mentions_are_unrelated_in_configured_repository(self) -> None:
        for context in (False, True):
            if context:
                gate.hook({**PROMPT, "cwd": str(self.project)}, home=self.home)
            for tool in ("Bash", "PowerShell"):
                for command in UNRELATED_SCRIPT_COMMANDS:
                    with self.subTest(context=context, tool=tool, command=command):
                        self.assertIsNone(
                            gate.hook(
                                {
                                    "cwd": str(self.project),
                                    "session_id": PROMPT["session_id"],
                                    "agent_type": "task-flow:implementer",
                                    "tool_name": tool,
                                    "tool_input": {"command": command},
                                },
                                home=self.home,
                            )
                        )
        self.assertFalse((self.task / "gates.jsonl").exists())

    def test_script_invocations_require_context_only_for_workflow_agents(self) -> None:
        commands = (
            "uv run --script plugins/task-flow/scripts/implement.py",
            "python plugins/task-flow/scripts/gates.py",
            "python3 plugins/task-flow/scripts/task_record.py render",
            'uv run --script "plugins/task-flow/scripts/prune_task.py"',
            r"py plugins\task-flow\scripts\implement.py",
            'python "C:/a directory/plugins/task-flow/scripts/task_record.py" render',
            "plugins/task-flow/scripts/gates.py",
        )
        for tool in ("Bash", "PowerShell"):
            for command in commands:
                payload = {
                    "cwd": str(self.project),
                    "tool_name": tool,
                    "tool_input": {"command": command},
                }
                with self.subTest(tool=tool, command=command):
                    self.assertIsNone(gate.hook(payload, home=self.home))
                    self.assertIn(
                        "context",
                        gate.hook(
                            {**payload, "agent_type": "task-flow:implementer"},
                            home=self.home,
                        ),
                    )

    def test_hook_rejects_malformed_payload_objects(self) -> None:
        self.assertIn("object", gate.hook([], home=self.home))
        self.assertIn(
            "object",
            gate.hook({**MAIN_LAUNCH, "tool_input": []}, home=self.home),
        )

    def test_unrelated_prompt_passes_invalid_policy_and_clears_context(self) -> None:
        gate.hook({**PROMPT, "cwd": str(self.project)}, home=self.home)
        self.config.write_text("max_rounds = 0\n", encoding="utf-8")
        self.assertIsNone(
            gate.hook(
                {**PROMPT, "cwd": str(self.project), "prompt": "unrelated request"},
                home=self.home,
            )
        )
        self.assertEqual(list((self.project / ".work").glob(".gate-context-*")), [])


class RebaseTest(GateFixture):
    def setUp(self) -> None:
        super().setUp()
        self.refs = {"main", "task/parent", "task/sibling", "epic-base"}
        self.git_calls = []
        fake = patch("phase_gate.subprocess.run", side_effect=self.git)
        fake.start()
        self.addCleanup(fake.stop)
        self.run = self.project / ".work" / "_autopilot" / "r1"
        self.run.mkdir(parents=True)
        self.link_run()

    def git(self, argv: list[str], **kwargs: object) -> object:
        self.git_calls.append((argv, kwargs))
        self.assertEqual(argv[0], "git")
        self.assertTrue(argv[-1].startswith("refs/heads/"))
        exists = argv[-1].removeprefix("refs/heads/") in self.refs
        return gate.subprocess.CompletedProcess(
            argv, 0 if argv[1] == "check-ref-format" or exists else 1
        )

    def parent(self, task_id: str, phase: str) -> None:
        directory = self.task.parent / task_id
        directory.mkdir(exist_ok=True)
        (directory / "status.md").write_text(
            f"# Status: {task_id}\n- phase: {phase}\n## History\n", encoding="utf-8"
        )
        (directory / "plan.md").write_text(PLAN, encoding="utf-8")

    def link_run(
        self, *, epic: bool = False, root: bool = False, parent_phase: str = "done"
    ) -> None:
        self.parent("parent", parent_phase)
        self.parent("sibling", "done")
        deps = "-" if root else "parent"
        source = "- source: epic\n- epic: demo\n" if epic else ""
        (self.run / "run.md").write_text(
            f"# Autopilot: r1\n- mode: full\n- base: main\n- budget: none\n- planning-tokens: 0\n- continue-past: none\n- bootstrap: none\n{source}- state: running\n## Tasks\n| # | Task | Depends on | Tokens used |\n| --- | --- | --- | --- |\n| 1 | parent | - | 0 |\n| 2 | sibling | - | 0 |\n| 3 | t1 | {deps} | 0 |\n## History\n- fixture\n",
            encoding="utf-8",
        )
        if epic:
            directory = self.task.parent / "_epics" / "demo"
            directory.mkdir(parents=True, exist_ok=True)
            value = epics.Epic(
                epic_id="demo",
                title="T",
                goal="G",
                base="epic-base",
                state="active",
                delivery="per-task",
                active_run="r1",
                tasks=[
                    epics.EpicTask(number=1, task_id="parent", depends=None),
                    epics.EpicTask(number=2, task_id="sibling", depends=None),
                    epics.EpicTask(
                        number=3, task_id="t1", depends=None if root else "parent"
                    ),
                ],
                history=["- 2020-01-05T00:00:00Z created — fixture"],
            )
            (directory / "epic.md").write_text(
                epics.render_epic(value), encoding="utf-8", newline="\n"
            )

    def test_start_approval_survives_stack_and_epic_dispatch(self) -> None:
        for epic, root, expected in (
            (False, False, "task/sibling"),
            (True, False, "task/parent"),
            (True, True, "epic-base"),
        ):
            with self.subTest(epic=epic, root=root):
                (self.task / "gates.jsonl").unlink(missing_ok=True)
                self.set_state("planned", 0)
                self.link_run(epic=epic, root=root, parent_phase="approved")
                self.approve()
                original = gate.log_entries(self.task)[-1]["attempt"]
                self.parent("parent", "done")
                self.user_action("grant", ceiling=4)
                self.call("rebase", base=expected)
                before = (self.task / "status.md").read_bytes()
                repeat = self.call("rebase", base=expected)
                self.assertEqual(before, (self.task / "status.md").read_bytes())
                self.assertEqual(repeat["attempt"], original)
                self.assertEqual(gate.status(self.task)[1]["base"], expected)
                self.call("transition", target="baseline", worktree=".work/t1/worktree")
                self.call("baseline", number=0)
                self.evidence(0)
                self.call("prepare", number=1)
                self.call("transition", target="implementing", number=1)
                self.call("implement", number=1)
                self.call("transition", target="checking")
                self.call("check", number=1)
                self.evidence(1)
                self.call("transition", target="reviewing")
                self.review(1)
                self.call("transition", target="review-approved")
                self.call("finish")
                snapshot = gate.record_digest(self.task)
                self.call("finish")
                self.assertEqual(snapshot, gate.record_digest(self.task))
                self.assertEqual(
                    sum(e["action"] == "approve" for e in gate.log_entries(self.task)),
                    1,
                )
                self.assertIn(b"fixture text that must survive", before)

    def test_cli_rebase_checks_explicit_local_refs(self) -> None:
        self.approve()
        with patch("builtins.print"):
            self.assertEqual(
                gate.main(
                    ["rebase", "--task-dir", str(self.task), "--base", "task/sibling"]
                ),
                0,
            )
        self.assertEqual(
            [call[0] for call in self.git_calls],
            [
                ["git", "check-ref-format", "refs/heads/task/t1"],
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/task/t1"],
                ["git", "check-ref-format", "refs/heads/task/sibling"],
                ["git", "show-ref", "--verify", "--quiet", "refs/heads/task/sibling"],
            ],
        )
        for _, kwargs in self.git_calls:
            # Windows runners expose temp dirs by 8.3 short name; compare resolved paths.
            self.assertEqual(Path(kwargs["cwd"]).resolve(), self.project.resolve())
            self.assertFalse(kwargs.get("shell", False))

    def test_new_prompt_grant_after_rebase_uses_resolved_attempt(self) -> None:
        self.approve()
        identity = gate.log_entries(self.task)[-1]["attempt"]
        self.call("rebase", base="task/sibling")
        self.assertIsNone(
            gate.hook(
                {
                    **PROMPT,
                    "cwd": str(self.project),
                    "prompt": "/task-flow:task-grant t1 4",
                },
                home=self.home,
            )
        )
        pending = json.loads(witness.location(self.task, "grant").read_bytes())
        self.assertEqual(pending["attempt"], identity)
        self.assertEqual(
            self.call("grant", source="user", ceiling=4)["attempt"], identity
        )

    def test_git_verification_errors_refuse_without_status_mutation(self) -> None:
        self.approve()
        before = (self.task / "status.md").read_bytes()
        for code in (1, 128):
            with (
                self.subTest(code=code),
                patch(
                    "phase_gate.subprocess.run",
                    return_value=gate.subprocess.CompletedProcess([], code),
                ),
            ):
                with self.assertRaisesRegex(gate.Refusal, "verify local branch"):
                    self.call("rebase", base="task/sibling")
                self.assertEqual(before, (self.task / "status.md").read_bytes())

    def test_refusals_preserve_status(self) -> None:
        self.approve()
        status = (self.task / "status.md").read_bytes()
        log = (self.task / "gates.jsonl").read_bytes()
        run = (self.run / "run.md").read_bytes()
        cases = (
            "approval",
            "base",
            "branch-missing",
            "membership",
            "ambiguous",
            "paused",
            "stopped",
            "parent",
            "breaker",
            "phase",
            "round",
            "metadata",
            "directory",
            "task-branch",
            "plan",
            "branch",
            "unrecorded-base",
            "retry",
        )
        for case in cases:
            with self.subTest(case=case):
                self.write("plan.md", PLAN)
                (self.task / "status.md").write_bytes(status)
                (self.task / "gates.jsonl").write_bytes(log)
                (self.run / "run.md").write_bytes(run)
                self.parent("parent", "done")
                self.refs.add("task/sibling")
                self.refs.discard("task/t1")
                other = self.run.parent / "r2"
                if other.exists():
                    (other / "run.md").unlink()
                    other.rmdir()
                if case == "approval":
                    (self.task / "gates.jsonl").write_bytes(b"")
                elif case == "branch-missing":
                    self.refs.remove("task/sibling")
                elif case == "membership":
                    (self.run / "run.md").write_text(
                        run.decode()
                        .replace("\r\n", "\n")
                        .replace("| 3 | t1 | parent | 0 |\n", ""),
                        encoding="utf-8",
                    )
                elif case == "ambiguous":
                    other.mkdir()
                    (other / "run.md").write_bytes(run)
                elif case in ("paused", "stopped", "breaker"):
                    change = (
                        run.decode()
                        .replace("\r\n", "\n")
                        .replace("state: running", f"state: {case}")
                        if case != "breaker"
                        else run.decode()
                        .replace("\r\n", "\n")
                        .replace("budget: none", "budget: 1")
                    )
                    (self.run / "run.md").write_text(change, encoding="utf-8")
                elif case == "parent":
                    self.parent("parent", "approved")
                elif case in (
                    "phase",
                    "round",
                    "metadata",
                    "branch",
                    "unrecorded-base",
                ):
                    old, new = {
                        "phase": ("phase: approved", "phase: baseline"),
                        "round": ("round: 0", "round: 1"),
                        "metadata": ("worktree: none", "worktree: .work/t1/worktree"),
                        "branch": ("branch: task/t1", "branch: task/other"),
                        "unrecorded-base": ("base: main", "base: task/parent"),
                    }[case]
                    self.write("status.md", status.decode().replace(old, new))
                elif case == "directory":
                    (self.task / "worktree").mkdir()
                elif case == "task-branch":
                    self.refs.add("task/t1")
                elif case == "plan":
                    self.write(
                        "plan.md", PLAN.replace("Exercise gates.", "Changed plan.")
                    )
                elif case == "retry":
                    archive = (
                        self.task.parent
                        / "_epics"
                        / "demo"
                        / "attempts"
                        / "t1"
                        / "attempt-1"
                    )
                    archive.mkdir(parents=True)
                    (archive / "spend.md").write_text("fixture", encoding="utf-8")
                    self.write(
                        "status.md",
                        status.decode().replace(
                            "- created",
                            "- created\n- round reset to 0 for attempt 1 of epic demo — authorized by the user ",
                        ),
                    )
                before = (self.task / "status.md").read_bytes()
                with self.assertRaises(gate.Refusal):
                    self.call(
                        "rebase", base="wrong" if case == "base" else "task/sibling"
                    )
                self.assertEqual(before, (self.task / "status.md").read_bytes())
                if (self.task / "worktree").exists():
                    (self.task / "worktree").rmdir()

    def test_append_failure_and_exact_publication_replay(self) -> None:
        self.approve()
        before = (self.task / "status.md").read_bytes()
        with (
            patch.object(gate, "append", side_effect=OSError("disk full")),
            self.assertRaises(gate.Refusal),
        ):
            self.call("rebase", base="task/sibling")
        self.assertEqual(before, (self.task / "status.md").read_bytes())
        with (
            patch.object(gate, "publish", side_effect=OSError("sharing")),
            self.assertRaises(gate.Refusal),
        ):
            self.call("rebase", base="task/sibling")
        self.assertEqual(before, (self.task / "status.md").read_bytes())
        pending = gate.log_entries(self.task)[-1]
        with self.assertRaises(gate.Refusal):
            self.call("transition", target="baseline")
        with self.assertRaises(gate.Refusal):
            self.call("rebase", base="task/parent")
        self.call("rebase", base="task/sibling")
        self.assertIn(pending["time"], gate.read(self.task / "status.md"))
        self.assertEqual(sum("rebase" in e for e in gate.log_entries(self.task)), 1)

    def test_tampered_links_and_identity_cannot_authorize(self) -> None:
        self.approve()
        self.call("rebase", base="task/sibling")
        status = (self.task / "status.md").read_bytes()
        original = gate.log_entries(self.task)
        for key, value in (
            ("new_attempt", "0" * 64),
            ("schema", "base-change/2"),
            ("branch", "task/other"),
        ):
            with self.subTest(key=key):
                entries = json.loads(json.dumps(original))
                entries[-1]["rebase"][key] = value
                self.write(
                    "gates.jsonl", "".join(json.dumps(e) + "\n" for e in entries)
                )
                with self.assertRaises(gate.Refusal):
                    self.call("transition", target="baseline")
                self.assertEqual(status, (self.task / "status.md").read_bytes())
        self.write("gates.jsonl", json.dumps(original[-1]) + "\n")
        with self.assertRaisesRegex(gate.Refusal, "disconnected"):
            self.call("baseline")
        self.write("gates.jsonl", "".join(json.dumps(e) + "\n" for e in original))
        self.write("plan.md", PLAN + "\nchanged plan bytes\n")
        with self.assertRaises(gate.Refusal):
            self.call("transition", target="baseline")
        self.write("plan.md", PLAN)
        for old, new in (
            ("base: task/sibling", "base: main"),
            ("base: task/sibling", "base: unrelated"),
            ("branch: task/t1", "branch: other"),
        ):
            self.write(
                "status.md",
                status.decode().replace(old, new) + "fabricated rebase History\n",
            )
            with self.assertRaises(gate.Refusal):
                self.call("transition", target="baseline")
            with self.assertRaises(gate.Refusal):
                self.call("rebase", base="task/sibling")

    def test_baseline_evidence_and_legacy_rebase(self) -> None:
        self.approve()
        self.call("transition", target="baseline")
        text = gate.read(self.task / "status.md").replace(
            "phase: baseline", "phase: approved"
        )
        self.write("status.md", text)
        with self.assertRaisesRegex(gate.Refusal, "baseline has already"):
            self.call("rebase", base="task/sibling")
        self.config.unlink()
        before = (self.task / "status.md").read_bytes()
        self.assertEqual(self.call("rebase", base="anything")["reason"], "legacy")
        self.assertEqual(before, (self.task / "status.md").read_bytes())

    def test_pending_change_revalidates_run_plan_and_identity(self) -> None:
        self.approve()
        with (
            patch.object(gate, "publish", side_effect=OSError("interrupted")),
            self.assertRaises(gate.Refusal),
        ):
            self.call("rebase", base="task/sibling")
        status = gate.read(self.task / "status.md")
        run = gate.read(self.run / "run.md")
        for case in ("plan", "branch", "retry", "base", "run", "missing-ref"):
            with self.subTest(case=case):
                self.write("plan.md", PLAN)
                self.write("status.md", status)
                (self.run / "run.md").write_text(run, encoding="utf-8")
                self.refs.add("task/sibling")
                if case == "plan":
                    self.write("plan.md", PLAN + "\nchanged\n")
                elif case == "branch":
                    self.write(
                        "status.md",
                        status.replace("branch: task/t1", "branch: changed"),
                    )
                elif case == "retry":
                    archive = (
                        self.task.parent
                        / "_epics"
                        / "demo"
                        / "attempts"
                        / "t1"
                        / "attempt-1"
                    )
                    archive.mkdir(parents=True, exist_ok=True)
                    (archive / "spend.md").write_text("fixture", encoding="utf-8")
                    self.write(
                        "status.md",
                        status.replace(
                            "- created",
                            "- created\n- round reset to 0 for attempt 1 of epic demo — authorized by the user ",
                        ),
                    )
                elif case == "base":
                    self.write(
                        "status.md", status.replace("base: main", "base: unrelated")
                    )
                elif case == "run":
                    (self.run / "run.md").write_text(
                        run.replace("state: running", "state: paused"), encoding="utf-8"
                    )
                else:
                    self.refs.remove("task/sibling")
                before = (self.task / "status.md").read_bytes()
                with self.assertRaises(gate.Refusal):
                    self.call("rebase", base="task/sibling")
                self.assertEqual(before, (self.task / "status.md").read_bytes())

    def test_changed_identity_refuses_baseline_and_protected_execution(self) -> None:
        self.approve()
        self.call("rebase", base="task/sibling")
        approved = gate.read(self.task / "status.md")
        self.evidence(0)
        archive = self.task.parent / "_epics" / "demo" / "attempts" / "t1" / "attempt-1"
        archive.mkdir(parents=True)
        (archive / "spend.md").write_text("fixture", encoding="utf-8")
        for case in ("plan", "branch", "retry", "base", "history"):
            with self.subTest(case=case):
                self.write("plan.md", PLAN + ("\nchanged\n" if case == "plan" else ""))
                text = approved
                if case == "branch":
                    text = text.replace("branch: task/t1", "branch: other")
                elif case == "retry":
                    text = text.replace(
                        "- created",
                        "- created\n- round reset to 0 for attempt 1 of epic demo — authorized by the user ",
                    )
                elif case in ("base", "history"):
                    text = text.replace("base: task/sibling", "base: unrelated")
                    if case == "history":
                        text = text.replace(
                            "\n## Log\n",
                            "\n- 2020-01-05T00:00:00Z rebase — task/sibling → unrelated; phase gate\n## Log\n",
                        )
                self.write("status.md", text)
                for action, kwargs in (
                    ("rebase", {"base": "task/sibling"}),
                    ("transition", {"target": "baseline"}),
                ):
                    before = (self.task / "status.md").read_bytes()
                    with self.assertRaisesRegex(gate.Refusal, "authorization"):
                        self.call(action, **kwargs)
                    self.assertEqual(before, (self.task / "status.md").read_bytes())
                self.write(
                    "status.md",
                    text.replace("phase: approved", "phase: implementing").replace(
                        "round: 0", "round: 1"
                    ),
                )
                with self.assertRaisesRegex(gate.Refusal, "authorization"):
                    self.call("implement", number=1)

    def test_human_tokens_grant_ceiling_and_multiple_links(self) -> None:
        self.approve()
        original = gate.log_entries(self.task)[-1]["attempt"]
        self.user_action("grant", ceiling=4)
        self.call("rebase", base="task/sibling")
        # A later dispatch may change the ordinary stack again before isolation.
        self.parent("sibling", "approved")
        # To keep dispatch eligible, remove the now-unfinished unrelated row.
        path = self.run / "run.md"
        path.write_text(
            gate.read(path)
            .replace("| 2 | sibling | - | 0 |\n", "")
            .replace("| 3 | t1", "| 2 | t1"),
            encoding="utf-8",
        )
        self.call("rebase", base="task/parent")
        text, fields = gate.status(self.task)
        self.assertEqual(
            gate.resolved_attempt(text, fields, self.task, gate.log_entries(self.task)),
            original,
        )
        # An old-base rollback cannot select an earlier link for replay.
        completed = gate.read(self.task / "status.md")
        self.write("status.md", completed.replace("base: task/parent", "base: main"))
        with self.assertRaises(gate.Refusal):
            self.call("rebase", base="task/sibling")
        self.write("status.md", completed)
        self.call("transition", target="baseline")
        self.evidence(0)
        self.call("transition", target="implementing", number=1)
        for number in range(1, 4):
            self.call("transition", target="checking")
            self.evidence(number)
            self.call("transition", target="reviewing")
            self.review(number, "CHANGES_REQUESTED")
            self.call("transition", target="implementing", number=number + 1)
        self.call("implement", number=4)
        self.call("transition", target="checking")
        self.evidence(4)
        self.call("transition", target="reviewing")
        self.review(4, "CHANGES_REQUESTED")
        with self.assertRaisesRegex(gate.Refusal, "round cap"):
            self.call("transition", target="implementing", number=5)


class PublicationSnapshotTest(GateFixture):
    def test_opt_in_snapshots_before_review_and_refuses_capture_failure(self) -> None:
        self.start()
        self.evidence(1)
        self.call("transition", target="checking")
        self.config.write_text(
            'preset = "supervised"\n[publish]\nenabled = true\nrepository = "owner/repo"\nremote = "origin"\ncredential_env = "TASK_FLOW_PUBLISH_TOKEN"\n',
            encoding="utf-8",
        )
        with patch(
            "publish_task.capture_review", side_effect=ValueError("source changed")
        ) as capture:
            with self.assertRaisesRegex(gate.Refusal, "source changed"):
                self.call("transition", target="reviewing")
            capture.assert_called_once()
        self.assertEqual(gate.status(self.task)[1]["phase"], "checking")
        with patch("publish_task.capture_review") as capture:
            self.call("transition", target="reviewing")
            capture.assert_called_once()


if __name__ == "__main__":
    unittest.main()
