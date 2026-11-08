# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Offline workflow gates: temporary task artifacts, no models or real sessions."""

import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
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
        self.call("approve", source="user", human=True)

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
        with self.assertRaisesRegex(gate.Refusal, "changed during"):
            self.call(
                "approve",
                source="user",
                human=True,
                approved_digest=gate.digest(b"older plan"),
            )
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
                    self.call("approve", human=True, source="user")
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
            {"source": "user", "human": True, "ceiling": 0},
        ):
            with self.assertRaises(gate.Refusal):
                self.call("grant", **args)
        self.call("grant", source="user", human=True, ceiling=4)
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

    def test_human_grant_confirmation_cannot_cross_attempts(self) -> None:
        self.start()
        text, fields = gate.status(self.task)
        confirmed = gate.attempt(text, fields, self.task)
        self.write("status.md", text.replace("- base: main", "- base: changed-base"))
        with self.assertRaisesRegex(gate.Refusal, "attempt changed"):
            self.call(
                "grant",
                source="user",
                human=True,
                ceiling=4,
                approved_attempt=confirmed,
            )
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
        self.call("approve", human=True, source="user")
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
            {"source": "user", "human": True, "ceiling": -1},
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
        self.call("grant", source="user", human=True, ceiling=3)
        self.call("transition", target="implementing", number=2)
        self.evidence(2)
        self.review(2, "CHANGES_REQUESTED", failed_ac=True)
        self.set_state("reviewing", 2)
        self.call("transition", target="escalated")
        self.call("grant", source="user", human=True, ceiling=4)
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
