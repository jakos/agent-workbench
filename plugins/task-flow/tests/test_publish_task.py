# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Offline publication fixtures: temporary git repos, fake GitHub, no logins."""

import contextlib
import copy
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import phase_gate
import publish_task as pub
import task_record
from test_phase_gate import GateFixture

CONFIG = """[publish]
enabled = true
repository = "owner/repo"
remote = "origin"
credential_env = "TASK_FLOW_PUBLISH_TOKEN"
"""


class PublicationFixture(GateFixture):
    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.project), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return result.stdout.strip()

    def setUp(self) -> None:
        super().setUp()
        self.config.write_text(
            'preset = "supervised"\n' + CONFIG, encoding="utf-8", newline="\n"
        )
        (self.project / ".claude/task-flow.records.toml").write_text(
            "[records]\nenabled = true\n", encoding="utf-8"
        )
        self.write("task.md", "# Task: t1\n## Goal\nExercise publication.\n")
        (self.project / ".gitignore").write_text(".work/\n", encoding="utf-8")
        self.git("init", "-b", "main")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.git("config", "core.autocrlf", "false")
        self.git("add", "-A")
        self.git("commit", "-m", "Base")
        self.git("checkout", "-b", "task/t1")
        self.git("remote", "add", "origin", "https://github.com/owner/repo.git")
        self.start()
        self.extra_source()
        (self.project / "src").mkdir()
        (self.project / "src/a.py").write_text("value = 1\n", encoding="utf-8")
        self.evidence(1)
        self.call("transition", target="checking")
        self.call("transition", target="reviewing")
        self.review(1)
        review = self.task / "review-1.md"
        review.write_text(
            review.read_text(encoding="utf-8").replace(
                "## Verdict:",
                "## Human attention\nrisk: high\nwhy: Publication touches credentials.\n- none\n## Verdict:",
            ),
            encoding="utf-8",
            newline="\n",
        )
        self.call("transition", target="review-approved")
        self.call("finish")
        self.write(
            "summary.md",
            "# Summary: t1\n## Goal\nExercise publication.\n## Changes\nSource.\n## Verification\nPassed.\n## Rounds\n1.\n## Out of scope\nMerging.\n",
        )
        record = task_record.build_record(self.task, risk_enabled=True)
        path = self.project / "docs/tasks/t1.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            task_record.render_record(record), encoding="utf-8", newline="\n"
        )
        self.git("add", "-A")
        self.git(
            "commit",
            "-m",
            "feat: fixture\n\n" + "\n".join(task_record.render_trailers(record)),
        )

    def extra_source(self) -> None:
        pass

    def prepare(self) -> dict:
        return pub.prepare(self.task, self.home)

    def publisher(self) -> pub.Publisher:
        return pub.Publisher(
            self.task,
            self.home,
            {"TASK_FLOW_PUBLISH_TOKEN": "sentinel-publication-secret"},
            self.root / "gh",
        )

    def grants(self, *actions: str) -> None:
        request = self.prepare()
        for action in actions:
            pub.save(
                self.task,
                "publication-grant-" + action + ".md",
                {"action": action, "request": pub.request_digest(request)},
            )


class PreflightTest(PublicationFixture):
    def test_prepare_is_stable_clean_and_bound(self) -> None:
        first = self.prepare()
        self.assertEqual(first, self.prepare())
        self.assertEqual(first["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual(first["config"]["repository"], "owner/repo")
        self.assertTrue(first["body"].startswith("risk: high\nwhy:"))
        self.assertIn(
            (self.project / "docs/tasks/t1.md").read_text(encoding="utf-8"),
            first["body"],
        )
        self.assertIsNone(first["parent"])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_pr_body_preserves_review_attention_text(self) -> None:
        attention = {
            "risk": "high",
            "why": "Publication touches credentials.",
            "attention": [
                {"path": "src/a.py", "line": 1, "what": "Inspect this code."}
            ],
        }
        with patch.object(pub.risk, "parse_attention", return_value=attention):
            body, _ = pub.record_body(
                self.task, self.project, phase_gate.status(self.task)[1]
            )
        self.assertIn("- src/a.py:1 \u2014 Inspect this code.", body)

    def test_branch_and_record_configuration_fail_closed(self) -> None:
        config = self.project / ".claude/task-flow.records.toml"
        config.write_text("[records]\nenabled = false\n", encoding="utf-8")
        with self.assertRaisesRegex(pub.Refusal, "records enabled"):
            self.prepare()
        self.git("reset", "--hard", "HEAD")
        self.git("checkout", "main")
        with self.assertRaisesRegex(pub.Refusal, "task branch"):
            self.prepare()

    def test_git_auth_helper_overrides_cached_helpers_without_token_arguments(
        self,
    ) -> None:
        publisher = self.publisher()
        with patch.object(pub, "run", return_value="") as run:
            pub.git(self.project, "push", env=publisher.env)
        argv = run.call_args.args[0]
        self.assertLess(
            argv.index("credential.helper="),
            argv.index("credential.https://github.com.helper=!gh auth git-credential"),
        )
        self.assertNotIn("sentinel-publication-secret", " ".join(argv))
        self.assertEqual(run.call_args.args[2]["GIT_NO_REPLACE_OBJECTS"], "1")

    def test_dirty_index_or_untracked_refuses(self) -> None:
        source = self.project / "src/a.py"
        for stage in (False, True):
            source.write_text("value = 2\n", encoding="utf-8")
            if stage:
                self.git("add", "-A")
            with self.assertRaisesRegex(pub.Refusal, "clean"):
                self.prepare()
            self.git("reset", "--hard", "HEAD")
        (self.project / "new.py").write_text("x = 1", encoding="utf-8")
        with self.assertRaisesRegex(pub.Refusal, "clean"):
            self.prepare()

    def test_committed_post_review_edits_refuse(self) -> None:
        (self.project / "src/a.py").write_text("value = 2\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "--amend", "--no-edit")
        with self.assertRaisesRegex(pub.Refusal, "post-review"):
            self.prepare()

    def test_changed_evidence_and_attempt_refuse(self) -> None:
        for name in ("plan.md", "task.md", "checks-1.md", "checks-0.md"):
            path = self.task / name
            before = path.read_bytes()
            path.write_bytes(before + b"\nchanged\n")
            with self.assertRaises(ValueError, msg=name):
                self.prepare()
            path.write_bytes(before)
        self.write("publication-review-1.md", "bad evidence")
        with self.assertRaises(ValueError):
            self.prepare()

    def test_changed_source_after_checks_cannot_acquire_review_snapshot(self) -> None:
        snapshot = pub.evidence(self.task, "publication-review-1.md")
        self.git("read-tree", "--reset", "-u", snapshot["tree"])
        (self.task / "publication-review-1.md").unlink()
        (self.project / "src/a.py").write_text(
            "changed_after_checks = True\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(pub.Refusal, "checked source changed"):
            pub.capture_review(self.task, home=self.home)
        with self.assertRaisesRegex(pub.Refusal, "checked source"):
            pub.capture_check(self.task, home=self.home)

    def test_valid_review_cannot_be_changed_after_approval(self) -> None:
        path = self.task / "review-1.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "Publication touches credentials.", "Another valid reason."
            ),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(pub.Refusal, "approved review.*stale"):
            self.prepare()

    def test_missing_approval_or_review_refuses(self) -> None:
        path = self.task / "gates.jsonl"
        original = path.read_bytes()
        rows = [
            line for line in original.splitlines() if b'"action": "approve"' not in line
        ]
        path.write_bytes(b"\n".join(rows) + b"\n")
        with self.assertRaisesRegex(phase_gate.Refusal, "authorization"):
            self.prepare()
        path.write_bytes(original)
        self.review(1, verdict="CHANGES_REQUESTED")
        with self.assertRaises(ValueError):
            self.prepare()

    def test_inconsistent_record_and_trailers_refuse(self) -> None:
        path = self.project / "docs/tasks/t1.md"
        path.write_text("wrong\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "--amend", "--no-edit")
        with self.assertRaisesRegex(pub.Refusal, "record"):
            self.prepare()
        self.git("checkout", "main", "--", ".claude/task-flow.policy.toml")

    def test_final_head_change_invalidates_request(self) -> None:
        self.prepare()
        self.git("commit", "--amend", "-m", "changed message")
        with self.assertRaises(pub.Refusal):
            self.prepare()

    def test_each_permission_is_separate_and_digest_bound(self) -> None:
        request = self.prepare()
        loaded = pub.policy.load(self.project, self.home)
        for action in ("push", "pr"):
            with self.assertRaisesRegex(pub.Refusal, "separate"):
                pub.permit(self.task, action, request, loaded, self.home)
        self.grants("push")
        pub.permit(self.task, "push", request, loaded, self.home)
        with self.assertRaises(pub.Refusal):
            pub.permit(self.task, "pr", request, loaded, self.home)
        changed = {**request, "head": "different"}
        with self.assertRaises(pub.Refusal):
            pub.permit(self.task, "push", changed, loaded, self.home)
        loaded["values"]["pr"] = "auto"
        pub.permit(self.task, "pr", request, loaded, self.home)

    def test_remote_destinations_fail_before_push(self) -> None:
        self.grants("push")
        publisher = self.publisher()
        for url in (
            "https://secret@github.com/owner/repo.git",
            "https://github.com/other/repo.git",
            "file:///tmp/other",
        ):
            self.git("remote", "set-url", "origin", url)
            with self.assertRaisesRegex(pub.Refusal, "remote"):
                publisher.revalidate("push")
        self.git("remote", "set-url", "origin", publisher.url)
        self.git("config", "remote.origin.pushurl", "https://github.com/other/repo.git")
        with self.assertRaises(pub.Refusal):
            publisher.revalidate("push")
        self.git("config", "--unset", "remote.origin.pushurl")
        self.git(
            "config", "url.https://elsewhere.invalid/.insteadOf", "https://github.com/"
        )
        with self.assertRaises(pub.Refusal):
            publisher.revalidate("push")

    def test_repository_http_auth_cannot_override_the_process_token(self) -> None:
        self.grants("push")
        self.git(
            "config",
            "http.https://github.com/owner/repo.git.extraHeader",
            "Authorization: Bearer cached-secret",
        )
        with self.assertRaisesRegex(pub.Refusal, "HTTP overrides") as caught:
            self.publisher().revalidate("push")
        self.assertNotIn("cached-secret", str(caught.exception))

    def test_authentication_is_only_process_scoped(self) -> None:
        publisher = pub.Publisher(
            self.task,
            self.home,
            {
                "TASK_FLOW_PUBLISH_TOKEN": "sentinel-publication-secret",
                "GH_TOKEN": "cached-secret",
                "GIT_ASKPASS": "evil",
                "GIT_TRACE": "1",
            },
            self.root / "gh",
        )
        self.assertEqual(publisher.env["GH_TOKEN"], "sentinel-publication-secret")
        self.assertNotIn("GIT_ASKPASS", publisher.env)
        self.assertNotIn("GIT_TRACE", publisher.env)
        self.assertNotIn("TASK_FLOW_PUBLISH_TOKEN", publisher.env)
        self.assertEqual(publisher.env["GH_CONFIG_DIR"], str(self.root / "gh"))
        self.assertEqual(publisher.env["HOME"], str(self.root / "gh"))
        self.assertEqual(publisher.env["USERPROFILE"], str(self.root / "gh"))
        for path in self.task.glob("publication*.md"):
            self.assertNotIn(
                "sentinel-publication-secret", path.read_text(encoding="utf-8")
            )


class NameSafetyTest(PublicationFixture):
    def extra_source(self) -> None:
        (self.project / " leading.py").write_text("value = 1\n", encoding="utf-8")

    def test_post_review_deletion_with_leading_space_is_not_hidden(self) -> None:
        (self.project / " leading.py").unlink()
        self.git("add", "-A")
        self.git("commit", "--amend", "--no-edit")
        with self.assertRaisesRegex(pub.Refusal, "post-review"):
            self.prepare()


class FinishFilesTest(PublicationFixture):
    def extra_source(self) -> None:
        import sdlc_status
        from test_sdlc_status import (
            README_TEMPLATE,
            TASK_WITH_DECLARATION,
            fixture_stages,
        )

        (self.project / "docs/task-flow").mkdir(parents=True)
        (self.project / "docs/evidence.txt").write_text("evidence\n", encoding="utf-8")
        (self.project / "README.md").write_text(
            README_TEMPLATE, encoding="utf-8", newline="\n"
        )
        (self.project / sdlc_status.SOURCE_RELPATH).write_text(
            sdlc_status.dump_source(fixture_stages()), encoding="utf-8", newline="\n"
        )
        self.write("task.md", TASK_WITH_DECLARATION.replace("fixture", "t1"))
        with contextlib.redirect_stdout(io.StringIO()):
            sdlc_status.cmd_render(self.project)

    def test_exact_deterministic_finish_is_allowed_and_other_readme_edits_refuse(
        self,
    ) -> None:
        import sdlc_status

        with contextlib.redirect_stdout(io.StringIO()):
            sdlc_status.cmd_finish(self.project, self.task / "task.md")
        self.git("add", "-A")
        self.git("commit", "--amend", "--no-edit")
        self.assertEqual(self.prepare()["head"], self.git("rev-parse", "HEAD"))
        (self.task / "publication-request.md").unlink()
        with (self.project / "README.md").open("a", encoding="utf-8") as handle:
            handle.write("arbitrary post-review edit\n")
        self.git("add", "-A")
        self.git("commit", "--amend", "--no-edit")
        with self.assertRaisesRegex(pub.Refusal, "post-review"):
            self.prepare()


class FakeGitHub:
    def __init__(self, publisher: pub.Publisher) -> None:
        self.publisher = publisher
        self.rows = []
        self.calls = []
        self.fail_labels = False
        self.base = "main"
        self.checks_pass = False
        self.merge = "merge-sha"

    def __call__(
        self, endpoint: str, payload: dict | None = None, method: str = "GET"
    ) -> object:
        self.calls.append((endpoint, copy.deepcopy(payload), method))
        if "/pulls?" in endpoint:
            return copy.deepcopy(self.rows)
        if endpoint.endswith("/pulls") and method == "POST":
            self.rows = [
                {
                    "number": 12,
                    "node_id": "node12",
                    "state": "open",
                    "body": payload["body"],
                    "draft": payload["draft"],
                    "head": {
                        "ref": self.publisher.branch,
                        "sha": self.publisher.request["head"],
                        "repo": {"full_name": "owner/repo"},
                    },
                    "base": {
                        "ref": payload["base"],
                        "sha": self.base,
                        "repo": {"full_name": "owner/repo"},
                    },
                    "merge_commit_sha": self.merge,
                }
            ]
            return copy.deepcopy(self.rows[0])
        if endpoint.endswith("/labels"):
            if self.fail_labels:
                raise pub.Refusal("label failure")
            return []
        if method == "PATCH":
            self.rows[0]["base"]["ref"] = "main"
            return copy.deepcopy(self.rows[0])
        if endpoint.endswith("required_status_checks"):
            return {"contexts": ["test", "lint"], "checks": []}
        if "/check-runs?" in endpoint:
            return {
                "check_runs": [
                    {
                        "name": name,
                        "status": "completed",
                        "conclusion": "success" if self.checks_pass else "failure",
                    }
                    for name in ("test", "lint")
                ]
            }
        if "/status?" in endpoint:
            return {"statuses": []}
        if endpoint == "graphql":
            self.rows[0]["draft"] = False
            return {}
        raise AssertionError(endpoint)


class NetworkTest(PublicationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.grants("push", "pr")
        self.pub = self.publisher()
        self.api = FakeGitHub(self.pub)
        self.pub.api = self.api
        self.network = []
        self.original_evidence = pub.evidence
        self.original_git = pub.git

        def fake_git(worktree: Path, *args: str, **kwargs: object) -> str:
            if args[0] in ("push", "fetch"):
                self.network.append((args, kwargs))
                return ""
            return self.original_git(worktree, *args, **kwargs)

        patcher = patch.object(pub, "git", side_effect=fake_git)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_exact_push_and_idempotent_pr_with_labels(self) -> None:
        self.pub.publish()
        self.pub.publish()
        self.assertEqual(
            len(
                [
                    c
                    for c in self.api.calls
                    if c[0].endswith("/pulls") and c[2] == "POST"
                ]
            ),
            1,
        )
        args, kwargs = self.network[0]
        self.assertEqual(
            args,
            (
                "push",
                "--no-verify",
                "--no-follow-tags",
                "--recurse-submodules=no",
                "--",
                "https://github.com/owner/repo.git",
                self.pub.request["head"] + ":refs/heads/task/t1",
            ),
        )
        self.assertEqual(kwargs["env"]["GH_TOKEN"], "sentinel-publication-secret")
        labels = [c[1] for c in self.api.calls if c[0].endswith("/labels")]
        self.assertEqual(labels[0], {"labels": ["agent", "risk:high"]})
        self.assertTrue(pub.evidence(self.task, "publication-receipt.md")["labeled"])

    def test_partial_failure_resumes_existing_pr(self) -> None:
        self.api.fail_labels = True
        with self.assertRaises(pub.Refusal):
            self.pub.publish()
        self.assertEqual(pub.evidence(self.task, "publication-receipt.md")["pr"], 12)
        self.api.fail_labels = False
        self.pub.publish()
        self.assertEqual(len(self.api.rows), 1)

    def test_push_grant_does_not_allow_pr(self) -> None:
        (self.task / "publication-grant-pr.md").unlink()
        with self.assertRaises(pub.Refusal):
            self.pub.publish()
        self.assertEqual(len(self.network), 1)
        self.assertFalse(self.api.calls)

    def test_conflicting_pr_refuses(self) -> None:
        self.pub.publish()
        for mutate in (
            lambda r: r["base"].update(ref="other"),
            lambda r: r["head"].update(sha="changed"),
            lambda r: r["head"]["repo"].update(full_name="other/repo"),
        ):
            original = copy.deepcopy(self.api.rows)
            mutate(self.api.rows[0])
            with self.assertRaises(pub.Refusal):
                self.pub.publish()
            self.api.rows = original

    def test_merged_intermediate_pr_is_not_delivery(self) -> None:
        self.pub.publish()
        self.api.rows[0].update(state="closed", merged=True)
        self.pub.fetched_main = lambda: self.git("rev-parse", "main")
        self.assertFalse(self.pub.delivery())
        self.assertFalse(pub.evidence(self.task, "publication-receipt.md")["delivered"])
        self.git("update-ref", "refs/heads/main", self.pub.request["head"])
        self.assertTrue(self.pub.delivery())
        self.assertEqual(self.prepare(), self.pub.request)

    def test_head_change_before_mutation_refuses(self) -> None:
        self.git("commit", "--amend", "-m", "changed")
        with self.assertRaises(pub.Refusal):
            self.pub.publish()
        self.assertFalse(self.network)

    def test_child_starts_draft_and_retargets_in_separate_operation(self) -> None:
        # Parent binding is covered separately; fake only the persisted parent lookup.
        self.pub.request["parent"] = "parent"
        self.pub.request["parent_head"] = "parent-sha"
        self.pub.revalidate = lambda action: None
        self.pub.publish()
        self.assertTrue(self.api.rows[0]["draft"])
        self.assertEqual(self.api.rows[0]["base"]["ref"], "task/parent")
        self.pub.fetched_main = lambda: "main-sha"
        self.pub.contains = lambda commit, main: False
        with patch.object(
            pub,
            "evidence",
            side_effect=lambda task, name: (
                {"task": "parent", "head": "parent-sha"}
                if task.name == "parent"
                else self.original_evidence(task, name)
            ),
        ):
            with self.assertRaisesRegex(pub.Refusal, "not delivered"):
                self.pub.advance()
            self.pub.contains = lambda commit, main: True
            self.pub.advance()
            self.assertEqual(self.api.rows[0]["base"]["ref"], "main")
            self.assertTrue(self.api.rows[0]["draft"])
        self.assertFalse(any(call[0] == "graphql" for call in self.api.calls))

    def test_child_ready_requires_current_merge_and_successful_required_checks(
        self,
    ) -> None:
        self.pub.request["parent"] = "parent"
        self.pub.request["parent_head"] = "parent-sha"
        self.pub.revalidate = lambda action: None
        self.pub.publish()
        row = self.api.rows[0]
        row["base"].update(ref="main", sha="main-sha")
        self.pub.fetched_main = lambda: "main-sha"
        self.pub.contains = lambda commit, main: True
        fetched = ["main-sha"]

        def merged_git(worktree: Path, *args: str, **kwargs: object) -> str:
            if args[0] == "fetch":
                fetched[0] = "merge-sha"
                return ""
            if args == ("rev-parse", "FETCH_HEAD"):
                return fetched[0]
            if args == ("show", "-s", "--format=%P", "merge-sha"):
                return "main-sha " + self.pub.request["head"]
            return self.original_git(worktree, *args, **kwargs)

        with (
            patch.object(
                pub,
                "evidence",
                side_effect=lambda task, name: (
                    {"task": "parent", "head": "parent-sha"}
                    if task.name == "parent"
                    else self.original_evidence(task, name)
                ),
            ),
            patch.object(pub, "git", side_effect=merged_git),
        ):
            with self.assertRaisesRegex(pub.Refusal, "checks.*not passed"):
                self.pub.advance()
            self.assertTrue(row["draft"])
            fetched[0] = "main-sha"
            self.api.checks_pass = True
            self.pub.advance()
            self.assertFalse(row["draft"])

    def test_timeout_and_authentication_failures_are_redacted(self) -> None:
        for error in (
            subprocess.TimeoutExpired(
                ["gh"], 120, output="sentinel-publication-secret"
            ),
            OSError("sentinel-publication-secret"),
        ):
            with patch.object(subprocess, "run", side_effect=error):
                with self.assertRaises(pub.Refusal) as caught:
                    pub.run(["gh", "api"], self.project, {})
                self.assertNotIn("sentinel-publication-secret", str(caught.exception))
        with patch.object(
            subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 1, "sentinel-publication-secret", "sentinel-publication-secret"
            ),
        ):
            with self.assertRaises(pub.Refusal) as caught:
                pub.run(["gh"], self.project, {})
            self.assertNotIn("sentinel-publication-secret", str(caught.exception))


class CliBoundaryTest(unittest.TestCase):
    def test_cli_refuses_before_starting_any_process(self) -> None:
        import cli_driver

        with patch.object(subprocess, "Popen") as process:
            with self.assertRaises(cli_driver.CliRefused) as caught:
                cli_driver.run_cli(
                    command=["model"],
                    prompt="non-secret handoff",
                    prompt_via="stdin",
                    usage_format="none",
                    timeout_seconds=1,
                    worktree=Path.cwd(),
                    env={"TASK_FLOW_PUBLISH_TOKEN": "sentinel-publication-secret"},
                )
            process.assert_not_called()
        self.assertNotIn("sentinel-publication-secret", str(caught.exception))

    def test_api_tools_exclude_all_authentication_variables(self) -> None:
        import implement

        with patch.dict(
            os.environ,
            {
                "TASK_FLOW_PUBLISH_TOKEN": "sentinel-publication-secret",
                "GH_TOKEN": "sentinel-publication-secret",
                "GITHUB_TOKEN": "sentinel-publication-secret",
            },
        ):
            self.assertNotIn(
                "sentinel-publication-secret", implement.child_env().values()
            )


class RunnerIsolationTest(unittest.TestCase):
    def test_runner_fails_before_model_contact_and_does_not_echo_secret(self) -> None:
        import implement
        import role_run

        for module in (implement, role_run):
            out = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {"TASK_FLOW_PUBLISH_TOKEN": "sentinel-publication-secret"},
                ),
                patch.object(module, "prepare") as prepare,
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(out),
            ):
                self.assertEqual(module.main([]), 2)
                prepare.assert_not_called()
            self.assertNotIn("sentinel-publication-secret", out.getvalue())


if __name__ == "__main__":
    unittest.main()
