# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Check plugin documentation, the usage guide and status-labelled roadmap."""

import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]


def prose(text: str) -> str:
    """Exclude fenced examples from link and heading inspection."""
    lines = []
    fence = ""
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = ""
        elif marker:
            fence = marker[1]
        else:
            lines.append(line)
    return "\n".join(lines)


def inline_link_targets(text: str, title: str) -> list[str]:
    """Read complete inline destinations, balancing bare destination parentheses."""
    targets = []
    for opening in re.finditer(r"!?\[[^]\n]+\]\(\s*", text):
        start = opening.end()
        if start == len(text):
            continue
        if text[start] == "<":
            angle = re.match(r"<([^>\n]+)>", text[start:])
            if not angle:
                continue
            target = angle[1]
            end = start + angle.end()
        else:
            end = start
            depth = 0
            while end < len(text) and not text[end].isspace():
                char = text[end]
                if char == "\\" and end + 1 < len(text):
                    end += 2
                    continue
                if char == "(":
                    depth += 1
                elif char == ")":
                    if depth == 0:
                        break
                    depth -= 1
                end += 1
            if depth:
                continue
            target = text[start:end]
        if target and re.match(r"(?:\s+" + title + r")?\s*\)", text[end:]):
            targets.append(re.sub(r"\\([()\\])", r"\1", target))
    return targets


def link_targets(text: str) -> list[str]:
    """Collect inline and reference destinations, including titled/angle links."""
    text = prose(text)
    title = r"""(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\))"""
    destination = r"\s*(<[^>\n]+>|\S+)(?:\s+" + title + r")?\s*"
    targets = inline_link_targets(text, title)
    definitions = {
        key.casefold(): value
        for key, value in re.findall(
            r"^ {0,3}\[([^]\n]+)\]:" + destination + r"$", text, re.MULTILINE
        )
    }
    for label, reference in re.findall(r"\[([^]\n]+)\]\[([^]\n]*)\]", text):
        targets.append(definitions.get((reference or label).casefold(), ""))
    # Checking definitions also covers shortcut references without treating inline
    # code such as [roles] as documentation links.
    targets.extend(definitions.values())
    return [target.strip("<>") for target in targets if target]


def heading_anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    for heading in re.findall(
        r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", prose(text), re.MULTILINE
    ):
        heading = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", heading)
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        anchor = slug
        number = 0
        while anchor in anchors:
            number += 1
            anchor = f"{slug}-{number}"
        anchors.add(anchor)
    return anchors


class DocumentationTest(unittest.TestCase):
    def test_session_authorization_skills_and_navigation(self) -> None:
        plugin = ROOT / "plugins" / "task-flow"
        for name in ("task-approve", "task-grant"):
            text = (plugin / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            frontmatter = text.split("---", 2)[1]
            self.assertIn(f"name: {name}\n", frontmatter)
            self.assertIn("disable-model-invocation: true", frontmatter)
            for required in (
                "--source user",
                "--task-dir",
                "--terminal",
                "five minutes",
                "attempt",
                "single-use",
                "current plan",
            ):
                self.assertIn(required, text)
            self.assertIn(
                f"/task-flow:{name} <task-id>",
                (plugin / "README.md").read_text(encoding="utf-8"),
            )
        orchestration = (plugin / "skills/workflow/orchestration.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "Three-platform Claude authorization smoke check",
            "Windows, macOS and Linux",
            ".work/.approval-witnesses/",
            "status replacement",
            "consumed nonce",
            "--terminal",
            "user-prompt",
            "no progress",
            "circuit",
        ):
            self.assertIn(required, orchestration)
        self.assertNotIn("The prompt hook captures task context only", orchestration)

    def assert_readme(self, root: Path) -> None:
        text = (root / "README.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 100)
        self.assertIsNone(
            re.search(r"\b(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b", text)
        )
        self.assertNotIn("<!--", text)
        for required in (
            "dev-baseline",
            "bootstrap",
            "Python tooling",
            "formatting",
            "debugging",
            "reviewer",
            "task-flow",
            "AI-native",
            "fresh-context roles",
            "file-based handoffs",
            "plan approval",
            "deterministic baseline",
            "independent criterion review",
            "policy-controlled autonomy",
            "## Install",
            "/plugin marketplace add jakos/agent-workbench",
            "/plugin install dev-baseline@agent-workbench",
            "/plugin install task-flow@agent-workbench",
            "## License",
            "[MIT](LICENSE)",
        ):
            self.assertIn(required, text)
        self.assertIn("docs/how-to.md", link_targets(text), "missing guide link")
        self.assertTrue((root / "docs/how-to.md").is_file(), "missing guide")
        self.assertRegex(
            text, r"(?s)```mermaid\s+flowchart\s+.+?```", "missing roadmap"
        )
        self.assertIn("Legend:", text, "missing roadmap legend")
        for status in ("Finished", "In progress", "Waiting / unfinished", "Planned"):
            self.assertIn(status, text)

    def assert_guide(self, root: Path) -> None:
        path = root / "docs/how-to.md"
        self.assertTrue(path.is_file(), "missing guide")
        text = path.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 160)
        for required in (
            "Prerequisites",
            "dev-baseline",
            "@code-reviewer",
            "/task-flow:task-doctor",
            "/task-flow:task-run",
            "approve",
            "baseline",
            "/task-flow:task-check",
            "/task-flow:task-review",
            "Resume",
            "/task-flow:task-finish",
            "three rounds",
            "policy-controlled",
            "no publication permission",
            "## Roadmap evidence",
            "unattended-isolation-2",
        ):
            self.assertIn(required, text)

    def assert_links(self, paths: list[Path]) -> None:
        for path in paths:
            for target in link_targets(path.read_text(encoding="utf-8")):
                url = urlsplit(target)
                if url.scheme or url.netloc:
                    continue
                destination = (
                    (path.parent / unquote(url.path)).resolve() if url.path else path
                )
                if destination.suffix.lower() != ".md":
                    continue
                self.assertTrue(destination.is_file(), f"{path}: broken link {target}")
                if url.fragment:
                    self.assertIn(
                        unquote(url.fragment),
                        heading_anchors(destination.read_text(encoding="utf-8")),
                        f"{path}: broken fragment {target}",
                    )

    def test_readme_describes_plugins_and_status_labelled_roadmap(self) -> None:
        self.assert_readme(ROOT)

    def test_usage_guide_is_bounded_and_covers_the_task_loop(self) -> None:
        self.assert_guide(ROOT)

    def test_local_markdown_documentation_links_resolve(self) -> None:
        paths = [
            ROOT / "README.md",
            ROOT / "GETTING-STARTED.md",
            ROOT / "docs/how-to.md",
        ]
        paths.extend((ROOT / "plugins").rglob("*.md"))
        paths.extend((ROOT / ".claude").rglob("*.md"))
        self.assert_links([path for path in paths if path.is_file()])

    def test_missing_guide_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "README.md").write_text(
                (ROOT / "README.md").read_text(encoding="utf-8"), encoding="utf-8"
            )
            with self.assertRaisesRegex(AssertionError, "missing guide"):
                self.assert_readme(root)
            with self.assertRaisesRegex(AssertionError, "missing guide"):
                self.assert_guide(root)

    def test_missing_roadmap_or_legend_is_rejected(self) -> None:
        original = (ROOT / "README.md").read_text(encoding="utf-8")
        variants = (
            (re.sub(r"(?s)```mermaid.*?```", "", original), "missing roadmap"),
            (original.replace("Legend:", "Key:"), "missing roadmap legend"),
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs").mkdir()
            (root / "docs/how-to.md").write_text("# Guide\n", encoding="utf-8")
            for text, message in variants:
                with self.subTest(message=message):
                    (root / "README.md").write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(AssertionError, message):
                        self.assert_readme(root)

    def test_empty_guide_content_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs").mkdir()
            (root / "docs/how-to.md").write_text("# Guide\n", encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "Prerequisites"):
                self.assert_guide(root)

    def test_broken_relative_documentation_links_and_fragments_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs").mkdir()
            page = root / "docs/how-to.md"
            target = root / "README.md"
            target.write_text("# Install\n# Install\n", encoding="utf-8")
            for link in ("missing.md", "../README.md#missing", "#missing"):
                with self.subTest(link=link):
                    page.write_text(f"[Read]({link})\n", encoding="utf-8")
                    with self.assertRaisesRegex(AssertionError, "broken"):
                        self.assert_links([page])
            page.write_text(
                '[Install](<../README.md#install-1> "Setup")\n'
                "[Setup][install]\n[install]: ../README.md#install\n"
                "[External](https://example.invalid/missing.md#missing)\n"
                "```markdown\n[Example](missing.md)\n```\n"
                "~~~markdown\n[Example](missing.md)\n~~~\n"
                "`docs/task-flow/sdlc-status.toml` config; "
                "`docs/task-flow/ai-native-sdlc.md` output; `docs/tasks/<id>.md` record.\n",
                encoding="utf-8",
            )
            self.assert_links([page])

    def test_titled_documentation_links_validate_destinations_and_fragments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            page = root / "page.md"
            (root / "target.md").write_text("# Setup\n", encoding="utf-8")
            for title in ('"Setup"', "'Setup'", "(Setup)"):
                for template in (
                    "[Read]({target} {title})\n",
                    "[Read](<{target}> {title})\n",
                    "[Read][setup]\n[setup]: {target} {title}\n",
                    "[Read][setup]\n[setup]: <{target}> {title}\n",
                ):
                    for target, error in (
                        ("missing.md", "broken link"),
                        ("target.md#missing", "broken fragment"),
                        ("target.md#setup", None),
                    ):
                        with self.subTest(
                            title=title, template=template, target=target
                        ):
                            page.write_text(
                                template.format(target=target, title=title),
                                encoding="utf-8",
                            )
                            self.assertIn(target, link_targets(page.read_text("utf-8")))
                            if error:
                                with self.assertRaisesRegex(AssertionError, error):
                                    self.assert_links([page])
                            else:
                                self.assert_links([page])

    def test_parenthesized_reference_destinations_validate_files_and_fragments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            page = root / "page.md"
            for filename in ("target(guide).md", "target(nested(guide)).md"):
                (root / filename).write_text("# Setup\n", encoding="utf-8")
                for definition in (
                    "{target}",
                    '{target} "Setup"',
                    "{target} 'Setup'",
                    "{target} (Setup)",
                    "<{target}>",
                    '<{target}> "Setup"',
                ):
                    for target, error in (
                        (filename.replace("target", "missing"), "broken link"),
                        (f"{filename}#missing", "broken fragment"),
                        (f"{filename}#setup", None),
                        (filename, None),
                    ):
                        with self.subTest(definition=definition, target=target):
                            page.write_text(
                                "[Read][guide]\n[guide]: "
                                + definition.format(target=target)
                                + "\n",
                                encoding="utf-8",
                            )
                            if error:
                                with self.assertRaisesRegex(AssertionError, error):
                                    self.assert_links([page])
                            else:
                                self.assert_links([page])
                            self.assertIn(target, link_targets(page.read_text("utf-8")))

    def test_parenthesized_inline_destinations_validate_files_and_fragments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            page = root / "page.md"
            for filename in ("target(guide).md", "target(nested(guide)).md"):
                (root / filename).write_text("# Setup\n", encoding="utf-8")
                for template in (
                    "[Read]({target})\n",
                    '[Read]({target} "Setup")\n',
                    "[Read]({target} 'Setup')\n",
                    "[Read]({target} (Setup))\n",
                    "[Read](<{target}>)\n",
                ):
                    for target, error in (
                        (filename.replace("target", "missing"), "broken link"),
                        (f"{filename}#missing", "broken fragment"),
                        (f"{filename}#setup", None),
                        (filename, None),
                    ):
                        with self.subTest(template=template, target=target):
                            page.write_text(
                                template.format(target=target), encoding="utf-8"
                            )
                            if error:
                                with self.assertRaisesRegex(AssertionError, error):
                                    self.assert_links([page])
                            else:
                                self.assert_links([page])
                            self.assertIn(target, link_targets(page.read_text("utf-8")))


if __name__ == "__main__":
    unittest.main()
