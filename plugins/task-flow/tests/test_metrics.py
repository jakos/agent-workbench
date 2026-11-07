# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Offline fixtures: all git histories and local task evidence are temporary."""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import metrics
import policy


class MetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.run_git("init", "-b", "main")
        self.run_git("config", "user.name", "Fixture")
        self.run_git("config", "user.email", "fixture@example.invalid")
        self.run_git("config", "commit.gpgsign", "false")
        self.run_git("config", "tag.gpgsign", "false")
        self.first = self.commit("2020-01-05", "initial")
        self.second = self.commit("2020-01-12", "fix: candidate only")
        self.release = self.commit("2020-01-19", "release")
        self.run_git("tag", "task-flow-1")
        self.run_git(
            "tag", "-a", "task-flow-1-copy", "-m", "annotation", date="2022-11-27"
        )
        self.last = self.commit("2020-03-01", "revert: candidate only")
        self.run_git("tag", "task-flow-2")
        self.run_git("tag", "other-3")
        self.cutoff = metrics.timestamp("2020-04-12T00:00:00Z")

    def run_git(self, *args: str, date: str = "2020-01-05") -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": date + "T00:00:00Z",
            "GIT_COMMITTER_DATE": date + "T00:00:00Z",
        }
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            encoding="utf-8",
            env=env,
        ).stdout.strip()

    def commit(self, date: str, message: str) -> str:
        return self.commit_file(date, message, "file.txt", message)

    def commit_file(self, date: str, message: str, path: str, text: str) -> str:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.run_git("add", path)
        self.run_git("commit", "-m", message, date=date)
        return self.run_git("rev-parse", "HEAD")

    def snapshot(
        self, incidents: Path | None = None, local: bool = False, weeks: int = 14
    ) -> dict:
        return metrics.build(
            self.repo,
            "HEAD",
            self.cutoff,
            weeks,
            "task-flow-",
            incidents,
            self.tasks if local else None,
            "2020-04-12",
        )

    def ledger(self, extra: str = "") -> Path:
        path = self.root / "incidents.toml"
        path.write_text(
            'version = 1\n[[incidents]]\nid = "incident-1"\ndeployment = "task-flow-1"\nfailed_at = "2020-01-26T00:00:00Z"\nrecovered_at = "2020-02-09T00:00:00Z"\nrework = ["task-flow-2"]\n'
            + extra,
            encoding="utf-8",
        )
        return path

    def totals(self, snapshot: dict) -> dict:
        return metrics.aggregate(
            snapshot,
            self.cutoff - timedelta(weeks=snapshot["settings"]["weeks"]),
            self.cutoff,
        )

    def cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with (
            patch.object(Path, "home", return_value=self.home),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(out),
        ):
            code = metrics.main([*args, "--repo", str(self.repo)])
        return code, out.getvalue()

    def local_task(self, identity: str = "example", rounds: int = 1) -> Path:
        task = self.tasks / identity
        task.mkdir()
        (task / "status.md").write_text(
            f"# Status: {identity}\n- phase: done\n- round: {rounds}\n",
            encoding="utf-8",
        )
        (task / "review-1.md").write_text("## Verdict: APPROVED\n", encoding="utf-8")
        return task

    def record(self, identity: str = "example", rounds: int = 1) -> None:
        self.commit_file(
            "2020-03-08",
            f"complete\n\nTask-Id: {identity}\nTask-Rounds: {rounds}\nTask-Implementer: codex/model",
            f"docs/tasks/{identity}.md",
            f"# Task record: {identity}\n- schema: task-record/1\n- task: {identity}\n- rounds: {rounds}\n- final-verdict: APPROVED\n- round 1: APPROVED — implementer codex/model\n",
        )

    def test_tags_reachability_assignment_and_target_dates(self) -> None:
        self.run_git("tag", "task-flow-tree", "HEAD^{tree}")
        self.run_git("checkout", "--orphan", "unrelated")
        self.commit("2020-01-26", "unrelated")
        self.run_git("tag", "task-flow-unreachable")
        self.run_git("checkout", "main")
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot["deploys"]), 3)
        self.assertEqual(snapshot["deploys"][1]["timestamp"], "2020-01-19T00:00:00Z")
        identities = [c for d in snapshot["deploys"] for c in d["changes"]]
        self.assertEqual(len(identities), 4)
        self.assertEqual(len(set(identities)), 4)
        self.assertEqual(snapshot["deploys"][1]["changes"], ())
        # Pinning an earlier revision excludes subsequent releases and changes.
        _, changes, deployments, _ = metrics.git_events(
            self.repo, self.release, "task-flow-", self.cutoff
        )
        self.assertEqual(len(changes), 3)
        self.assertEqual(len(deployments), 2)

    def test_merges_assign_each_change_once(self) -> None:
        self.run_git("checkout", "-b", "side", self.second)
        branch = self.commit_file("2020-01-26", "side", "side.txt", "side")
        self.run_git("checkout", "main")
        self.run_git("merge", "--no-ff", "side", "-m", "merge", date="2020-03-08")
        self.run_git("tag", "task-flow-3")
        snapshot = self.snapshot()
        assigned = [c for d in snapshot["deploys"] for c in d["changes"]]
        self.assertIn(branch, assigned)
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(len(assigned), len(snapshot["changes"]))

    def test_formulas_and_exact_boundaries(self) -> None:
        snapshot = self.snapshot(self.ledger())
        total = self.totals(snapshot)
        self.assertEqual(total["lead_time_seconds"], {"value": 302400, "count": 4})
        self.assertEqual(
            total["deployment_frequency_per_day"],
            {"value": 3 / 98, "numerator": 3, "denominator": 98},
        )
        self.assertEqual(
            total["change_fail_rate"],
            {"value": 1 / 3, "numerator": 1, "denominator": 3},
        )
        self.assertEqual(
            total["failed_deployment_recovery_seconds"], {"value": 1209600, "count": 1}
        )
        self.assertEqual(total["deployment_rework_rate"], total["change_fail_rate"])
        first = metrics.aggregate(
            snapshot, self.cutoff - timedelta(days=98), self.cutoff - timedelta(days=49)
        )
        second = metrics.aggregate(
            snapshot, self.cutoff - timedelta(days=49), self.cutoff
        )
        self.assertEqual((first["deployments"], second["deployments"]), (2, 1))
        self.assertEqual((first["changes"], second["changes"]), (3, 1))
        # Timestamp exactly at exclusive cutoff is omitted.
        self.commit("2020-04-12", "boundary")
        self.run_git("tag", "task-flow-cutoff")
        self.assertEqual(self.totals(self.snapshot())["deployments"], 3)
        self.assertEqual(
            metrics.iso("2020-04-12T01:00:00+01:00"), "2020-04-12T00:00:00Z"
        )

    def test_negative_durations_and_unresolved_incidents(self) -> None:
        self.commit("2020-03-29", "future parent")
        self.commit("2020-03-08", "clock skew release")
        self.run_git("tag", "task-flow-skew")
        total = self.totals(self.snapshot())
        self.assertEqual(total["negative_durations"], 1)
        self.assertEqual(total["lead_time_seconds"]["count"], 5)
        ledger = self.ledger()
        ledger.write_text(
            ledger.read_text(encoding="utf-8").replace(
                'recovered_at = "2020-02-09T00:00:00Z"\n', ""
            ),
            encoding="utf-8",
        )
        total = self.totals(self.snapshot(ledger))
        self.assertEqual(total["unresolved_incidents"], 1)
        self.assertEqual(total["failed_deployment_recovery_seconds"]["count"], 0)

    def test_missing_ledger_is_unavailable_empty_supplied_is_observed(self) -> None:
        total = self.totals(self.snapshot())
        for key in (
            "change_fail_rate",
            "failed_deployment_recovery_seconds",
            "deployment_rework_rate",
        ):
            self.assertIsNone(total[key])
        path = self.root / "empty.toml"
        path.write_text("version = 1\n", encoding="utf-8")
        self.assertEqual(
            self.totals(self.snapshot(path))["change_fail_rate"]["value"], 0
        )
        empty = metrics.aggregate(
            self.snapshot(), self.cutoff, self.cutoff + timedelta(days=49)
        )
        self.assertEqual(empty["deployments"], 0)
        self.assertIsNone(empty["lead_time_seconds"]["value"])
        self.assertEqual(empty["deployment_frequency_per_day"]["value"], 0)

    def test_incident_refusals_preserve_publication(self) -> None:
        ledger = self.ledger()
        original = ledger.read_text(encoding="utf-8")
        output = self.root / "report.md"
        output.write_bytes(b"preserve\r\n")
        invalid = [
            original + "unknown = 1\n",
            original + original[original.index("[[incidents]]") :],
            original.replace("task-flow-1", "missing"),
            original.replace("2020-01-26T00:00:00Z", "bad"),
            original.replace("2020-02-09T00:00:00Z", "2020-01-19T00:00:00Z"),
            original.replace("version = 1", "version = true"),
            original.replace('["task-flow-2"]', '["missing"]'),
            original.replace('"2020-01-26T00:00:00Z"', '"2020-01-26T00:00:00"'),
        ]
        for text in invalid:
            with self.subTest(text=text):
                ledger.write_text(text, encoding="utf-8")
                code, _ = self.cli(
                    "publish",
                    "--output",
                    str(output),
                    "--cutoff",
                    "2020-04-12",
                    "--incidents",
                    str(ledger),
                )
                self.assertEqual(code, 2)
                self.assertEqual(output.read_bytes(), b"preserve\r\n")

    def test_task_deduplication_cohorts_and_completion(self) -> None:
        self.record()
        task = self.local_task()
        (task / "impl-1.json").write_text(
            json.dumps({"model": "model", "cost_usd": 2}), encoding="utf-8"
        )
        snapshot = self.snapshot(local=True)
        self.assertEqual(len(snapshot["tasks"]), 1)
        self.assertEqual(snapshot["tasks"][0]["completed_at"], "2020-03-08T00:00:00Z")
        self.assertEqual(snapshot["tasks"][0]["cohort"], "agent")
        self.assertEqual(self.totals(snapshot)["agent_metrics"]["tasks"], 1)
        self.local_task("unfinished")
        snapshot = self.snapshot(local=True)
        self.assertEqual(snapshot["inventory"]["local_unfinished_tasks"], 1)
        self.assertEqual(self.totals(snapshot)["agent_metrics"]["tasks"], 1)
        self.assertEqual(self.totals(snapshot)["change_cohorts"]["unknown"], 4)
        self.assertEqual(metrics.cohort_for([]), "unknown")
        self.assertEqual(metrics.cohort_for(["human/human"]), "non-agent")
        # A later edit cannot move the original completion date.
        self.commit_file(
            "2020-03-15",
            "edit",
            "docs/tasks/example.md",
            (self.repo / "docs/tasks/example.md").read_text(encoding="utf-8")
            + "\nextra\n",
        )
        self.assertEqual(
            self.snapshot()["tasks"][0]["completed_at"], "2020-03-08T00:00:00Z"
        )

    def test_record_cohorts_ignore_implementer_prose(self) -> None:
        for identity, implementer, expected, with_trailer in (
            ("unknown", "unknown/unknown", "unknown", False),
            ("human", "human/human", "non-agent", False),
            ("agreement", "human/human", "non-agent", True),
        ):
            with self.subTest(identity=identity):
                message = "complete"
                if with_trailer:
                    message += (
                        f"\n\nTask-Id: {identity}\nTask-Rounds: 1"
                        f"\nTask-Implementer: {implementer}"
                    )
                self.commit_file(
                    "2020-03-08",
                    message,
                    f"docs/tasks/{identity}.md",
                    f"# Task record: {identity}\n- schema: task-record/1\n"
                    f"- task: {identity}\n- rounds: 1\n- final-verdict: APPROVED\n"
                    f"- round 1: APPROVED — implementer {implementer}\n"
                    "## Goal\nThe implementer launches the task.\n"
                    "## Out of scope\nThe implementer must preserve scope.\n",
                )
                tasks = {task["id"]: task for task in self.snapshot()["tasks"]}
                self.assertEqual(tasks[identity]["cohort"], expected)
                if with_trailer:
                    self.assertEqual(len(tasks[identity]["sources"]), 2)

    def test_contradictory_tasks_and_invocations_are_refused(self) -> None:
        self.record()
        task = self.local_task(rounds=2)
        with self.assertRaisesRegex(ValueError, "contradictory"):
            self.snapshot(local=True)
        (task / "status.md").write_text(
            "# Status: example\n- round: 1\n", encoding="utf-8"
        )
        role = {
            "task_id": "example",
            "role": "reviewer",
            "invocation_id": "same",
            "cost_usd": 1,
        }
        (task / "role-reviewer-a.json").write_text(json.dumps(role), encoding="utf-8")
        (task / "role-reviewer-b.json").write_text(json.dumps(role), encoding="utf-8")
        self.assertEqual(len(self.snapshot(local=True)["tasks"][0]["calls"]), 1)
        role["cost_usd"] = 2
        (task / "role-reviewer-b.json").write_text(json.dumps(role), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "contradictory"):
            self.snapshot(local=True)

    def test_agent_formulas_cost_completeness_and_unknowns(self) -> None:
        tasks = [
            {
                "rounds": 1,
                "reviews": {"1": "APPROVED"},
                "escalated": False,
                "calls": {
                    "impl-1": {"role": "implementer", "cost": 2, "known": 2},
                    "reviewer:a": {"role": "reviewer", "cost": None, "known": 1},
                },
            },
            {
                "rounds": 3,
                "reviews": {"1": "CHANGES_REQUESTED"},
                "escalated": True,
                "calls": {
                    "impl-1": {"role": "implementer", "cost": None, "known": None}
                },
            },
            {"calls": {}},
        ]
        result = metrics.agent_metrics(tasks)
        self.assertEqual(result["first_round_acceptance"], metrics.ratio(1, 2))
        self.assertEqual(result["rounds_per_task"], metrics.ratio(4, 2))
        self.assertEqual(result["escalations"], metrics.ratio(1, 2))
        self.assertEqual(result["known_cost_per_task_usd"], metrics.ratio(3, 1))
        self.assertEqual(result["reviewer_cost_share"], metrics.ratio(1, 3))
        self.assertEqual(
            result["cost_coverage"],
            {
                "calls": 3,
                "known_calls": 2,
                "complete_calls": 1,
                "tasks_with_cost": 1,
                "tasks_with_complete_recorded_calls": 0,
                "population_tasks": 3,
                "label": "partial",
            },
        )
        for key in ("plan_approval_without_edits", "supervision_burden", "escape_rate"):
            self.assertIsNone(result[key])
        empty = metrics.agent_metrics([])
        self.assertIsNone(empty["known_cost_per_task_usd"]["value"])
        self.assertIsNone(empty["reviewer_cost_share"]["value"])

    def test_local_costs_summary_and_trailer_prose(self) -> None:
        task = self.local_task()
        (task / "impl-1.json").write_text(
            '{"kind": "claude", "cost_usd": null, "cost_known_usd": 0}',
            encoding="utf-8",
        )
        snapshot = self.snapshot(local=True)
        self.assertEqual(snapshot["tasks"][0]["cohort"], "agent")
        self.assertIsNone(snapshot["tasks"][0]["calls"]["impl-1"]["known"])
        summary = task / "summary.md"
        summary.write_text(
            "## Rounds\nOne round; approved.\n## Out of scope\nFuture escalations.\n",
            encoding="utf-8",
        )
        self.assertFalse(metrics.summary_escalation(task, "APPROVED"))
        summary.write_text(
            "## Rounds\nStopped: no progress; escalated.\n", encoding="utf-8"
        )
        self.assertTrue(metrics.summary_escalation(task, "CHANGES_REQUESTED"))
        summary.write_text("## Goal\nEscalation handling\n", encoding="utf-8")
        self.assertIsNone(metrics.summary_escalation(task, "APPROVED"))
        self.commit(
            "2020-03-08",
            "explain trailers\n\nTask-Id: quoted\nTask-Implementer: codex/model\n\nThis is quoted documentation.",
        )
        self.assertEqual(self.snapshot()["tasks"], [])
        self.assertEqual(self.snapshot()["changes"][-1]["cohort"], "unknown")
        tasks = {}
        metrics.merge_task(tasks, "example", {"reviews": {"1": "APPROVED"}})
        metrics.merge_task(
            tasks, "example", {"reviews": {"1": "APPROVED", "2": "APPROVED"}}
        )
        self.assertEqual(len(tasks["example"]["reviews"]), 2)
        with self.assertRaisesRegex(ValueError, "contradictory"):
            metrics.merge_task(
                tasks, "example", {"reviews": {"1": "CHANGES_REQUESTED"}}
            )

    def test_malformed_task_identities_and_record_rounds(self) -> None:
        self.record()
        path = self.repo / "docs/tasks/example.md"
        original = path.read_text(encoding="utf-8")
        self.commit_file(
            "2020-03-15",
            "bad record",
            "docs/tasks/example.md",
            original + "- round 1: CHANGES_REQUESTED\n",
        )
        with self.assertRaisesRegex(ValueError, "contradictory"):
            self.snapshot()
        task = self.local_task()
        (task / "status.md").write_text(
            "# Status: other\n- round: 1\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            metrics.local_task(task)
        (task / "impl-other.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "identity"):
            metrics.local_calls(task)

    def test_cli_process_does_not_create_import_caches(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        for source in Path(metrics.__file__).parent.glob("*.py"):
            shutil.copyfile(source, scripts / source.name)
        before = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        env = {**os.environ, "USERPROFILE": str(self.home), "HOME": str(self.home)}
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        result = subprocess.run(
            [
                sys.executable,
                str(scripts / "metrics.py"),
                "report",
                "--repo",
                str(self.repo),
                "--cutoff",
                "2020-04-12",
            ],
            env=env,
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        missing_cutoff = subprocess.run(
            [
                sys.executable,
                str(scripts / "metrics.py"),
                "publish",
                "--repo",
                str(self.repo),
                "--output",
                str(self.root / "missing.md"),
            ],
            env=env,
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(missing_cutoff.returncode, 2)
        after = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)

    def test_cli_readonly_publication_and_deterministic_snapshot(self) -> None:
        def inventory() -> dict:
            return {
                str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*")
                if p.is_file()
            }

        before = inventory()
        code, report = self.cli("report", "--cutoff", "2020-04-12")
        self.assertEqual(code, 0, report)
        self.assertEqual(inventory(), before)
        self.assertEqual(
            len(
                [
                    line
                    for line in report.splitlines()
                    if re.match(r"^\| \d{4}-\d{2}-\d{2} \|", line)
                ]
            ),
            7,
        )
        self.assertIn("xychart-beta", report)
        snapshot = self.snapshot(weeks=7)
        self.assertEqual(
            metrics.render(snapshot), metrics.render(json.loads(json.dumps(snapshot)))
        )
        output = self.root / "report.md"
        output.write_text(
            "intro\n" + metrics.START + "\nstale\n" + metrics.END + "\ntail\n",
            encoding="utf-8",
        )
        before = inventory()
        code, message = self.cli(
            "publish", "--cutoff", "2020-04-12", "--output", str(output)
        )
        self.assertEqual(code, 0, message)
        after = inventory()
        self.assertEqual(
            [name for name in before if before[name] != after[name]], ["report.md"]
        )
        first = output.read_bytes()
        self.assertTrue(first.startswith(b"intro\n"))
        self.assertTrue(first.endswith(b"tail\n"))
        self.assertNotIn(b"\r", first)
        self.assertEqual(
            self.cli("publish", "--cutoff", "2020-04-12", "--output", str(output))[0], 0
        )
        self.assertEqual(output.read_bytes(), first)
        output.write_text(metrics.END + metrics.START, encoding="utf-8")
        self.assertEqual(
            self.cli("publish", "--cutoff", "2020-04-12", "--output", str(output))[0], 2
        )
        self.assertEqual(
            output.read_text(encoding="utf-8"), metrics.END + metrics.START
        )

    def test_shared_policy_strictness_and_cli_precedence(self) -> None:
        path = self.repo / policy.CONFIG
        path.parent.mkdir()
        path.write_text(
            '[metrics]\nenabled = false\nweeks = 3\ntag_prefix = "other-"\n',
            encoding="utf-8",
        )
        code, report = self.cli(
            "report",
            "--cutoff",
            "2020-04-12",
            "--weeks",
            "7",
            "--tag-prefix",
            "task-flow-",
        )
        self.assertEqual(code, 0)
        self.assertIn('"weeks": 7', report)
        self.assertIn('"eligible_tags": 3', report)
        ledger = self.ledger()
        shutil.copyfile(ledger, self.repo / "incidents.toml")
        path.write_text(
            '[metrics]\nenabled = true\nincident_path = "incidents.toml"\n',
            encoding="utf-8",
        )
        code, report = self.cli("report", "--cutoff", "2020-04-12")
        self.assertEqual(code, 0, report)
        self.assertIn('"incidents_supplied": true', report)
        empty = self.root / "empty.toml"
        empty.write_text("version = 1\n", encoding="utf-8")
        code, report = self.cli(
            "report", "--cutoff", "2020-04-12", "--incidents", str(empty)
        )
        self.assertEqual(code, 0, report)
        self.assertIn('"incidents": 0', report)
        for text in (
            "[metrics]\nunknown = true",
            "[metrics]\nenabled = 1",
            "[metrics]\nweeks = true",
            "[metrics]\nweeks = 0",
            '[metrics]\ntag_prefix = ""',
            '[metrics]\nincident_path = "../secret"',
            '"metrics.enabled" = true',
        ):
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(policy.ConfigError):
                policy.load(self.repo, self.home)
        self.assertEqual(self.cli("report", "--weeks", "0")[0], 2)


if __name__ == "__main__":
    unittest.main()
