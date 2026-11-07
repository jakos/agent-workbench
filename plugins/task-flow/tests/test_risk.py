# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Offline risk contract and complete-diff fixtures; no real task or Git state."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import risk

ENABLED = {"enabled": True, "paths": []}
SECTION = "## Human attention\nrisk: high\nwhy: Sensitive change.\n- src/x.py:1 — Check intent.\n## Verdict: APPROVED\n"


class ParsingTest(unittest.TestCase):
    def test_configs(self) -> None:
        self.assertFalse(risk.parse_config(None)["enabled"])
        self.assertFalse(risk.parse_config("[risk]\nenabled=false")["enabled"])
        self.assertTrue(risk.parse_config("[risk]\nenabled=true")["enabled"])
        for text in (
            '[risk]\nenabled="yes"',
            "[risk]\nenabled=true\nx=1",
            "[other]\nenabled=true",
            "[risk]\nenabled=true\n[paths]\nx=1",
        ):
            with self.subTest(text=text), self.assertRaises(risk.RiskError):
                risk.parse_config(text)
        for pattern in ("/etc/*", "../x", "a/../b", "C:/x", "a\\b", "~/x", "a//b"):
            with self.subTest(pattern=pattern), self.assertRaises(risk.RiskError):
                risk.parse_config(
                    "[risk]\nenabled=true\n[[paths]]\npattern="
                    + json.dumps(pattern)
                    + '\nlevel="high"'
                )
        for level in ('"critical"', "42", "true", "[]"):
            with self.assertRaises(risk.RiskError):
                risk.parse_config(
                    '[risk]\nenabled=true\n[[paths]]\npattern="src/**"\nlevel=' + level
                )

    def test_attention(self) -> None:
        self.assertIsNone(risk.parse_attention("## Verdict: APPROVED\n"))
        self.assertEqual(risk.parse_attention(SECTION)["risk"], "high")
        self.assertEqual(
            risk.parse_attention(
                SECTION.replace("- src/x.py:1 — Check intent.", "- none")
            )["attention"],
            [],
        )
        invalid = [
            SECTION.replace("high", "critical"),
            SECTION.replace("why: Sensitive change.", "why: "),
            SECTION.replace("why:", "risk: low\nwhy:"),
            SECTION.replace("src/x.py", "../x"),
            SECTION.replace(":1 —", ":0 —"),
            SECTION.replace("## Verdict:", "## Extra\n## Verdict:"),
            SECTION.replace(
                "- src/x.py:1 — Check intent.", "\n".join(["- src/x.py:1 — Check."] * 6)
            ),
            SECTION.replace(
                "- src/x.py:1 — Check intent.", "- none\n- src/x.py:1 — Check."
            ),
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(risk.RiskError):
                risk.parse_attention(text)
        assessment = {"enabled": True, "floor": "high", "files": {"src/x.py": 1}}
        risk.validate_attention(SECTION, assessment)
        for text in (
            SECTION.replace("high", "medium"),
            SECTION.replace("src/x.py", "other.py"),
            SECTION.replace(":1 —", ":2 —"),
            "## Verdict: APPROVED",
        ):
            with self.assertRaises(risk.RiskError):
                risk.validate_attention(text, assessment)


class SignalsTest(unittest.TestCase):
    def assess(
        self,
        path: str = "src/x.py",
        old: bytes = b"",
        new: bytes = b"x\n",
        old_path: str | None = None,
        config: dict | None = None,
    ) -> dict:
        return risk.inspect_changes([(path, old_path, old, new)], config or ENABLED)

    def test_boundaries(self) -> None:
        for count, level in (
            (299, "low"),
            (300, "medium"),
            (999, "medium"),
            (1000, "high"),
        ):
            self.assertEqual(self.assess(new=b"x\n" * count)["floor"], level)
        for count, level in ((9, "low"), (10, "medium"), (29, "medium"), (30, "high")):
            result = risk.inspect_changes(
                [(f"src/{i}.py", None, b"", b"x") for i in range(count)], ENABLED
            )
            self.assertEqual(result["floor"], level)
        self.assertEqual(
            self.assess(old=b"old\n" * 150, new=b"new\n" * 150)["changed_lines"], 300
        )

    def test_paths(self) -> None:
        for path in (
            "src/auth/login.py",
            "security/policy.py",
            "hooks/pre.py",
            "scripts/rules.py",
            ".github/workflows/ci.yml",
            ".gitlab-ci.yml",
            "scripts/release.py",
            ".claude/task-flow.release.toml",
            "ci/check.yml",
            "db/migrations/001.sql",
            "deploy/prod.yml",
            "Dockerfile",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.assess(path)["floor"], "high")
        config = {
            "enabled": True,
            "paths": [
                {"pattern": "src/**", "level": "high"},
                {"pattern": "**", "level": "low"},
            ],
        }
        self.assertEqual(self.assess(config=config)["floor"], "high")
        self.assertEqual(self.assess("hooks/a", config=config)["floor"], "high")
        self.assertEqual(
            self.assess("src/x.py", old_path="auth/old.py")["floor"], "high"
        )

    def test_line_signals(self) -> None:
        for path, old, new, code in [
            ("pyproject.toml", b"", b'dependencies = ["x"]', "added-dependency"),
            ("requirements.txt", b"", b"x==1", "added-dependency"),
            ("tests/test_x.py", b"assert ok\n", b"pass\n", "removed-assertion"),
            ("tests/test_x.py", b"def test_x(): pass", b"", "deleted-test"),
            ("tests/test_x.py", b"", b"@pytest.mark.xfail", "changed-test-skip"),
            ("tests/test_x.py", b'@unittest.skip("x")', b"", "changed-test-skip"),
            (
                "pyproject.toml",
                b"fail_under = 90",
                b"fail_under = 80",
                "reduced-coverage",
            ),
        ]:
            result = self.assess(path, old, new)
            self.assertEqual(result["floor"], "medium", code)
            self.assertIn(code, result["reasons"])
        self.assertEqual(
            self.assess("requirements.txt", new=b"# comment")["floor"], "low"
        )
        self.assertEqual(
            self.assess(
                "coverage.cfg", b"branches=90\nlines=50", b"branches=80\nlines=60"
            )["floor"],
            "medium",
        )
        self.assertEqual(
            self.assess("coverage.cfg", b"fail_under=80", b"fail_under=90")["floor"],
            "low",
        )
        self.assertEqual(
            self.assess("pyproject.toml", b"fail_under=80", b"fail_under=90")["floor"],
            "medium",
        )  # dependency file additions

    def test_secrets_binary_and_no_value_export(self) -> None:
        secret = "sk-" + "A" * 24
        result = self.assess(new=('api_key = "' + secret + '"').encode())
        self.assertEqual(result["floor"], "high")
        self.assertNotIn(secret, json.dumps(result))
        for new in (b"\0binary", b"\xff", None):
            result = risk.inspect_changes([("x", None, b"", new)], ENABLED)
            self.assertEqual(result["floor"], "high")
            self.assertIn("incomplete-analysis", result["reasons"])


class GitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.write(".gitignore", ".work/\n")
        self.write(risk.CONFIG, "[risk]\nenabled=true\n")
        self.write("old name ü.txt", "before\n")
        self.write("deleted.py", "old\n")
        self.git("add", ".")
        self.git("commit", "-m", "fixture base")
        self.task = self.repo / ".work" / "demo"
        self.task.mkdir(parents=True)
        (self.task / "status.md").write_text(
            "- base: main\n- round: 1\n- worktree: none\n", encoding="utf-8"
        )

    def git(self, *args: str) -> bytes:
        return risk.git(self.repo, *args)

    def write(self, name: str, value: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8", newline="\n")

    def test_complete_diff_and_pinned_config(self) -> None:
        self.git("mv", "old name ü.txt", "new name ü.txt")
        self.write("new name ü.txt", "before\nunstaged\n")
        (self.repo / "deleted.py").unlink()
        self.write("untracked ü.txt", "new\n")
        self.write(risk.CONFIG, "[risk]\nenabled=false\n")
        result = risk.assess(self.repo, self.task, self.repo, 1)
        self.assertTrue(result["enabled"])
        for name in (
            "old name ü.txt",
            "new name ü.txt",
            "deleted.py",
            "untracked ü.txt",
        ):
            self.assertIn(name, result["files"])
        self.assertEqual(result["files"]["deleted.py"], 1)
        review = SECTION.replace("src/x.py", "deleted.py")
        risk.validate_attention(review, result)
        with self.assertRaises(risk.RiskError):
            risk.assess(self.repo, self.task, self.repo, 2)
        with self.assertRaises(risk.RiskError):
            risk.assess(self.repo, self.task, self.task, 1)

    def test_cli_and_secret_redaction(self) -> None:
        secret = "ghp_" + "X" * 30
        self.write("new.txt", secret)
        argv = [
            "assess",
            "--repo",
            str(self.repo),
            "--task-dir",
            str(self.task),
            "--worktree",
            "none",
            "--round",
            "1",
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = risk.main(argv)
        self.assertEqual(code, 0)
        self.assertNotIn(secret, out.getvalue() + err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["floor"], "high")
        (self.task / "review-1.md").write_text(
            SECTION.replace("high", "low"), encoding="utf-8"
        )
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(risk.main(["validate", *argv[1:]]), 2)

    def test_disabled_and_missing_base_config_are_noops(self) -> None:
        for text in (None, "[risk]\nenabled=false\n"):
            if text is None:
                self.git("rm", risk.CONFIG)
            else:
                self.write(risk.CONFIG, text)
                self.git("add", risk.CONFIG)
            self.git("commit", "-m", "fixture configuration")
            # A task adding enabled config cannot activate itself, either.
            self.write(risk.CONFIG, "[risk]\nenabled=true\n")
            args = [
                "validate",
                "--repo",
                str(self.repo),
                "--task-dir",
                str(self.task),
                "--worktree",
                "none",
                "--round",
                "1",
            ]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(risk.main(args), 0)
            self.assertFalse(risk.assess(self.repo, self.task, self.repo, 1)["enabled"])

    def test_complete_enabled_review_validation(self) -> None:
        from test_role_contracts import REVIEW_PLAN, VALID_REVIEW, checks_fixture

        (self.task / "plan.md").write_text(REVIEW_PLAN, encoding="utf-8")
        (self.task / "checks-1.md").write_text(checks_fixture(), encoding="utf-8")
        review = VALID_REVIEW.replace(
            "## Verdict:",
            "## Human attention\nrisk: high\nwhy: Human judgement.\n- none\n## Verdict:",
        )
        path = self.task / "review-1.md"
        path.write_text(review, encoding="utf-8")
        args = [
            "validate",
            "--repo",
            str(self.repo),
            "--task-dir",
            str(self.task),
            "--worktree",
            "none",
            "--round",
            "1",
        ]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(risk.main(args), 0)
            path.write_text(
                review.replace("| AC1 | PASS", "| AC1 | FAIL"), encoding="utf-8"
            )
            self.assertEqual(risk.main(args), 2)

    def test_binary_and_malformed_configuration_refusal(self) -> None:
        (self.repo / "binary.dat").write_bytes(b"\x00\xff")
        result = risk.assess(self.repo, self.task, self.repo, 1)
        self.assertEqual(result["floor"], "high")
        self.assertIn("incomplete-analysis", result["reasons"])
        self.write(risk.CONFIG, "[risk]\nenabled=true\nunknown=1\n")
        self.git("add", risk.CONFIG)
        self.git("commit", "-m", "invalid fixture configuration")
        with self.assertRaises(risk.RiskError):
            risk.assess(self.repo, self.task, self.repo, 1)


if __name__ == "__main__":
    unittest.main()
