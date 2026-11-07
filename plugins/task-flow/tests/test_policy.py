# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Offline policy fixtures; no real home, repository state, model or login."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import policy


class PolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.home = Path(self.tmp.name) / "home"
        self.project.mkdir()
        self.home.mkdir()
        self.plan = self.project / "plan.md"
        self.task = self.project / "task"
        self.task.mkdir()
        self.set_plan()

    def write(self, text: str, user: bool = False) -> None:
        path = (self.home if user else self.project) / policy.CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def set_plan(
        self,
        rating: str = "plan-risk: low",
        profiles: str = "python: src/a.py",
        steps: str = "1. Change `src/a.py` (python).",
    ) -> None:
        self.plan.write_text(
            f"# Plan: Test\n## Profiles\n{profiles}\n## Steps\n{steps}\n## Risks / open questions\n{rating}\n",
            encoding="utf-8",
        )

    def load(self) -> dict:
        return policy.load(self.project, self.home)

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(Path, "home", return_value=self.home),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = policy.main([*args, "--project", str(self.project)])
        return code, out.getvalue(), err.getvalue()

    def test_legacy(self) -> None:
        loaded = self.load()
        self.assertEqual(loaded["mode"], "legacy")
        self.assertTrue(policy.allow_auto(loaded, "high"))
        for action in ("plan-approval", "push", "pr", "merge", "release"):
            self.assertFalse(policy.decide(action, loaded, self.project)["allowed"])
        settings = self.project / ".claude/task-flow.md"
        settings.parent.mkdir()
        settings.write_text("## Finish\n- push: yes\n- pr: ask\n", encoding="utf-8")
        self.assertTrue(policy.decide("push", loaded, self.project)["allowed"])
        self.assertFalse(policy.decide("pr", loaded, self.project)["allowed"])
        for args in (
            ("show",),
            ("validate",),
            ("plan-risk", "--plan", str(self.plan)),
            ("allow-auto", "--scope", "plan-approval", "--plan-risk", "high"),
            ("decide", "push"),
        ):
            code, out, _ = self.cli(*args)
            self.assertEqual(code, 0)
            self.assertIn("legacy", out)

    def test_exact_presets_and_sources(self) -> None:
        expected = {
            "supervised": ("ask", "ask", "ask", "ask", False, "semi"),
            "gated": ("auto-low-risk", "auto", "auto", "ask", False, "semi"),
            "autonomous": ("auto", "auto", "auto", "auto-low-risk", True, "full"),
        }
        for preset, row in expected.items():
            with self.subTest(preset=preset):
                self.write(f'preset = "{preset}"')
                loaded = policy.effective(self.load(), "active")
                values = loaded["values"]
                self.assertEqual(
                    tuple(
                        values[k]
                        for k in (
                            "plan_approval",
                            "push",
                            "pr",
                            "merge",
                            "isolation_required",
                            "autopilot.mode",
                        )
                    ),
                    row,
                )
                self.assertEqual(values["release"], "ask")
                self.assertEqual(values["max_rounds"], 3)
                self.assertIs(values["no_progress_stop"], True)
                self.assertIsNone(values["budget_tokens"])
                self.assertIsNone(values["budget_usd"])
                self.assertEqual(values["risk_paths"], [])
                self.assertEqual(set(loaded["sources"].values()), {f"preset:{preset}"})
                self.assertFalse(
                    policy.decide("release", loaded, self.project, isolation="active")[
                        "allowed"
                    ]
                )

    def test_all_overrides(self) -> None:
        self.write("""preset = "gated"
plan_approval = "auto"
max_rounds = 2
no_progress_stop = true
push = "ask"
pr = "ask"
merge = "auto"
release = "auto"
isolation_required = true
budget_tokens = 100
budget_usd = 2.5
risk_paths = ["src/private/*"]
[autopilot]
mode = "full"
""")
        loaded = self.load()
        self.assertEqual(set(loaded["sources"].values()), {"project", "preset:gated"})
        self.assertEqual(
            loaded["values"],
            {
                "plan_approval": "auto",
                "max_rounds": 2,
                "no_progress_stop": True,
                "push": "ask",
                "pr": "ask",
                "merge": "auto",
                "release": "auto",
                "isolation_required": True,
                "budget_tokens": 100,
                "budget_usd": 2.5,
                "risk_paths": ["src/private/*"],
                "autopilot.mode": "full",
                "metrics.enabled": False,
                "metrics.tag_prefix": "task-flow-",
                "metrics.weeks": 7,
                "metrics.incident_path": "",
            },
        )
        result = policy.decide("release", loaded, self.project, isolation="active")
        self.assertEqual(result["decision"], "auto (explicit)")

    def test_merge_and_sources(self) -> None:
        self.write(
            'push = "auto"\npr = "auto"\nmax_rounds = 1\nisolation_required = true\nbudget_tokens = 100\nbudget_usd = 2',
            user=True,
        )
        self.write(
            'push = "ask"\nmax_rounds = 2\nisolation_required = false\nbudget_tokens = 200\nbudget_usd = 3'
        )
        loaded = self.load()
        self.assertEqual(loaded["values"]["push"], "ask")
        self.assertEqual(loaded["sources"]["push"], "project")
        self.assertEqual(loaded["sources"]["pr"], "user")
        self.assertEqual(loaded["sources"]["merge"], "default")
        for key, value in (
            ("max_rounds", 1),
            ("isolation_required", True),
            ("budget_tokens", 100),
            ("budget_usd", 2),
        ):
            self.assertEqual(loaded["values"][key], value)
            self.assertEqual(loaded["sources"][key], "user")
        self.write(
            "max_rounds = 1\nisolation_required = true\nbudget_tokens = 50\nbudget_usd = 1"
        )
        self.write(
            "max_rounds = 3\nisolation_required = false\nbudget_tokens = 200\nbudget_usd = 3",
            user=True,
        )
        for key in ("max_rounds", "isolation_required", "budget_tokens", "budget_usd"):
            self.assertEqual(self.load()["sources"][key], "project")

    def test_invalid_cli(self) -> None:
        cases = {
            "unknown": "unknown = true",
            "table": "[table]\nx = 1",
            "preset": 'preset = "other"',
            "plan_approval": 'plan_approval = "yes"',
            "push": "push = true",
            "pr": 'pr = "yes"',
            "merge": 'merge = "later"',
            "release": "release = 1",
            "isolation_required": "isolation_required = 1",
            "budget_tokens": "budget_tokens = true",
            "budget_usd": "budget_usd = inf",
            "risk_paths": 'risk_paths = "src/*"',
            "autopilot": "[autopilot]\nother = true",
            "autopilot.mode": '[autopilot]\nmode = "auto"',
        }
        for key, text in cases.items():
            with self.subTest(key=key):
                self.write(text)
                code, _, err = self.cli("validate")
                self.assertEqual(code, 2)
                self.assertIn(key, err)
        for key, values in {
            "max_rounds": ("0", "4", "true", "2.5"),
            "no_progress_stop": ("false", "1"),
            "budget_tokens": ("0", "-1", "1.5"),
            "budget_usd": ("0", "-1", "nan", "true"),
        }.items():
            for value in values:
                self.write(f"{key} = {value}")
                code, _, err = self.cli("validate")
                self.assertEqual(code, 2)
                self.assertIn(key, err)
        for path in (
            "/etc/*",
            "../src/*",
            "src/../../x",
            "C:/x",
            "~/.ssh/*",
            "src/./x",
            "src//x",
            "",
        ):
            self.write("risk_paths = " + json.dumps([path]))
            code, _, err = self.cli("validate")
            self.assertEqual(code, 2, path)
            self.assertIn("risk_paths", err)

    def test_preset_merge_retains_stricter_user_limits(self) -> None:
        self.write('preset = "gated"\nmax_rounds = 1\nbudget_usd = 2', user=True)
        self.write('preset = "supervised"')
        loaded = self.load()
        self.assertEqual(loaded["values"]["max_rounds"], 1)
        self.assertEqual(loaded["sources"]["max_rounds"], "user")
        self.assertEqual(loaded["values"]["budget_usd"], 2)
        self.assertEqual(loaded["values"]["push"], "ask")
        self.assertEqual(loaded["sources"]["push"], "preset:supervised")

    def test_explicit_user_release_and_project_override(self) -> None:
        self.write('release = "auto"', user=True)
        self.assertEqual(
            policy.decide("release", self.load(), self.project)["decision"],
            "auto (explicit)",
        )
        self.assertEqual(self.load()["sources"]["release"], "user")
        self.write('release = "ask"')
        self.assertFalse(policy.decide("release", self.load(), self.project)["allowed"])

    def test_isolation(self) -> None:
        for text in (
            'preset = "autonomous"',
            "isolation_required = true",
            'preset = "autonomous"\nisolation_required = false',
        ):
            self.write(text)
            for args in (
                ("validate",),
                ("show",),
                ("decide", "push"),
                ("allow-auto", "--scope", "plan-approval", "--plan-risk", "low"),
            ):
                code, _, err = self.cli(*args)
                self.assertEqual(code, 2)
                self.assertIn("missing isolation backend", err)
                self.assertIn(self.cli(*args, "--isolation", "active")[0], (0, 1))

    def test_plan_risk(self) -> None:
        for line in ("", "plan-risk: low\nplan-risk: low", "plan-risk: unknown"):
            self.set_plan(line)
            self.assertEqual(policy.plan_risk(self.plan, self.load()), "high")
        for rating in policy.risk.LEVELS:
            self.set_plan(f"plan-risk: {rating}")
            self.assertEqual(policy.plan_risk(self.plan, self.load()), rating)
        self.set_plan(steps="1. Explain " + " ".join(f"`word{n}`" for n in range(20)))
        self.assertEqual(policy.plan_risk(self.plan, self.load()), "low")
        self.set_plan(steps="\n".join(f"{n}. Change it" for n in range(1, 11)))
        self.assertEqual(policy.plan_risk(self.plan, self.load()), "medium")
        self.set_plan(profiles="python: " + ", ".join(f"src/{n}.py" for n in range(15)))
        self.assertEqual(policy.plan_risk(self.plan, self.load()), "medium")
        for path in (
            "src/auth/check.py",
            "plugins/task-flow/hooks/x.py",
            "scripts/rules.py",
            ".github/workflows/check.yml",
            "src/migrations/x.sql",
            ".claude/task-flow.md",
            "deploy/x.yaml",
            "scripts/release.py",
        ):
            self.set_plan(profiles="python: " + path)
            self.assertEqual(policy.plan_risk(self.plan, self.load()), "high", path)
        self.set_plan(steps="1. Change `src/private/a.py`.")
        self.assertEqual(policy.plan_risk(self.plan, self.load()), "low")
        self.write('risk_paths = ["src/private/*"]')
        self.assertEqual(policy.plan_risk(self.plan, self.load()), "high")
        self.write("risk_paths = []")
        self.set_plan(profiles="config: .claude/task-flow.md")
        self.assertEqual(policy.plan_risk(self.plan, self.load()), "high")

    def test_auto_clamp(self) -> None:
        for preset, answers in (
            ("gated", (True, False, False)),
            ("supervised", (False, False, False)),
            ("autonomous", (True, True, True)),
        ):
            self.write(f'preset = "{preset}"')
            for rating, allowed in zip(policy.risk.LEVELS, answers):
                self.set_plan(f"plan-risk: {rating}")
                code, _, _ = self.cli(
                    "allow-auto",
                    "--scope",
                    "plan-approval",
                    "--plan",
                    str(self.plan),
                    "--isolation",
                    "active",
                )
                self.assertEqual(code, 0 if allowed else 1)
                result = policy.decide(
                    "plan-approval",
                    self.load(),
                    self.project,
                    self.plan,
                    isolation="active",
                )
                self.assertEqual(result["allowed"], allowed)

    def test_merge_uses_shared_final_review_parser(self) -> None:
        self.write('merge = "auto-low-risk"')
        (self.task / "status.md").write_text("- round: 2\n", encoding="utf-8")
        review = self.task / "review-2.md"
        for rating in ("unavailable", "invalid", "low", "medium", "high"):
            text = (
                "# Review\n"
                if rating == "unavailable"
                else f"# Review\n## Human attention\nrisk: {rating}\nwhy: Fixture\n- none\n## Verdict: APPROVED\n"
            )
            review.write_text(text, encoding="utf-8")
            with patch.object(
                policy.risk, "parse_attention", wraps=policy.risk.parse_attention
            ) as shared:
                result = policy.decide(
                    "merge", self.load(), self.project, task_dir=self.task
                )
                self.assertEqual(result["allowed"], rating == "low")
                shared.assert_called_once_with(text)
        self.assertFalse(policy.decide("merge", self.load(), self.project)["allowed"])

    def test_invalid_status_never_uses_a_stale_low_review(self) -> None:
        self.write('merge = "auto-low-risk"')
        (self.task / "review-2.md").write_text(
            "# Review\n## Human attention\nrisk: low\nwhy: Fixture\n- none\n## Verdict: APPROVED\n",
            encoding="utf-8",
        )
        for status in (
            "",
            "- round: 0\n",
            "- round: 4\n",
            "- round: 2\n- round: invalid\n",
        ):
            with self.subTest(status=status):
                (self.task / "status.md").write_text(status, encoding="utf-8")
                self.assertFalse(
                    policy.decide(
                        "merge", self.load(), self.project, task_dir=self.task
                    )["allowed"]
                )

    def test_metrics_table_defaults_merging_and_strict_types(self) -> None:
        self.assertFalse(self.load()["values"]["metrics.enabled"])
        self.write(
            '[metrics]\nenabled = true\nweeks = 4\ntag_prefix = "release-"', user=True
        )
        self.write(
            '[metrics]\nweeks = 7\nincident_path = ".claude/task-flow.incidents.toml"'
        )
        loaded = self.load()
        self.assertEqual(loaded["values"]["metrics.weeks"], 7)
        self.assertEqual(loaded["sources"]["metrics.weeks"], "project")
        self.assertEqual(loaded["sources"]["metrics.enabled"], "user")
        self.assertEqual(loaded["values"]["metrics.tag_prefix"], "release-")
        for value in (
            "[metrics]\nother = true",
            "[metrics]\nweeks = 53",
            '[metrics]\nenabled = "false"',
            '[metrics]\nincident_path = "/secret"',
            '[metrics]\nincident_path = "src/*.toml"',
        ):
            self.write(value)
            with self.assertRaises(policy.ConfigError):
                self.load()

    def test_output_is_versioned_and_safe(self) -> None:
        self.write('preset = "gated"\nrelease = "auto"')
        secret = "sensitive-value-for-test"
        with patch.dict("os.environ", {"POLICY_TEST_SECRET": secret}):
            code, out, err = self.cli("show", "--format", "json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["schema"], "policy/1")
        self.assertNotIn(secret, out + err)
        self.assertNotIn(str(self.home), out)
        self.assertEqual(
            data["files"][0]["path"], str((self.project / policy.CONFIG).resolve())
        )
        self.assertIn("auto (explicit)", self.cli("show")[1])
        self.write('push = "' + secret + '"')
        self.assertNotIn(secret, self.cli("validate")[2])


if __name__ == "__main__":
    unittest.main()
