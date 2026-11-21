# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Check plugin documentation, the usage guide and status-labelled roadmap."""

import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/task-flow/scripts"))

DEAD_ENDS = ROOT / "docs/task-flow-dead-ends.md"
DEAD_END_SECTIONS = ["Method", "Dead ends", "Self-hosting", "Follow-up epic"]
DEAD_END_FIELDS = (
    "Incident",
    "Enforced at",
    "How it blocks",
    "Automatic run",
    "Remedy",
    "Follow-up",
)
SELF_HOSTING_PHASES = (
    "hooks",
    "plan",
    "approve",
    "baseline",
    "implement",
    "check",
    "review",
    "finish",
    "release",
)
GOVERNING_COPIES = ("installed plugin", "root checkout", "task worktree")
REFERENCE = re.compile(r"`([^`\s]+?):(\d+)`")


def markdown_sections(text: str, level: str) -> list[tuple[str, list[str]]]:
    """Split lines into (heading, body) pairs at headings of exactly `level`."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if line.startswith(level + " "):
            sections.append((line[len(level) + 1 :].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def table_rows(lines: list[str]) -> list[list[str]]:
    """Return the cells of the first Markdown table, without its separator row."""
    rows = []
    for line in lines:
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not all(set(cell) <= set("-: ") for cell in cells):
                rows.append(cells)
        elif rows:
            break
    return rows


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
    def test_verification_workers_contract_and_template(self) -> None:
        plugin = ROOT / "plugins/task-flow"
        for name in ("README.md", "skills/workflow/orchestration.md"):
            text = (plugin / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                for phrase in (
                    "[gates] workers",
                    "positive integer",
                    "defaults to 1 (serial)",
                    "project file",
                    "overrides the user file",
                    "input order",
                    "Dependent commands require `workers = 1`",
                    "full shared-list parity",
                    "30 commands",
                ):
                    self.assertIn(phrase, text)
        template = (plugin / "templates/models.toml").read_text(encoding="utf-8")
        self.assertEqual(tomllib.loads(template)["gates"]["workers"], 1)
        self.assertIn("project overrides the user value", template)
        self.assertIn("Reports retain input order", template)
        project = (ROOT / ".claude/task-flow.models.toml").read_text(encoding="utf-8")
        self.assertEqual(tomllib.loads(project)["gates"]["workers"], 4)

    def test_claude_subagent_effort_contract(self) -> None:
        plugin = ROOT / "plugins/task-flow"
        levels = ("low", "medium", "high", "xhigh", "max")
        for name in ("README.md", "skills/workflow/orchestration.md"):
            text = (plugin / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertIn("[roles.effort]", text)
                for level in levels:
                    self.assertIn(f"`{level}`", text)
        orchestration = (plugin / "skills/workflow/orchestration.md").read_text(
            encoding="utf-8"
        )
        self.assertIn('"effort"', orchestration)
        self.assertIn("claude:opus, effort high", orchestration)
        self.assertIn("Pass the `effort` argument", orchestration)
        skill = (plugin / "skills/workflow/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`effort` key", skill)
        template = (plugin / "templates/models.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(template)
        self.assertEqual(
            set(parsed["roles"]),
            {"planner", "implementer", "checker", "reviewer", "backlog"},
        )
        self.assertNotIn("effort", parsed["roles"])
        self.assertIn("# [roles.effort]", template)
        for level in levels:
            self.assertIn(f'"{level}"', template)

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
            "user-prompt",
            "no progress",
            "circuit",
        ):
            self.assertIn(required, orchestration)
        self.assertNotIn("The prompt hook captures task context only", orchestration)
        for relative in (
            "README.md",
            "skills/task-approve/SKILL.md",
            "skills/task-grant/SKILL.md",
            "skills/task-plan/SKILL.md",
            "skills/task-run/SKILL.md",
            "skills/workflow/SKILL.md",
            "skills/workflow/orchestration.md",
        ):
            with self.subTest(page=relative):
                guidance = (plugin / relative).read_text(encoding="utf-8")
                self.assertNotIn("--terminal", guidance)
                self.assertIn("prompt hook is unavailable", guidance)
                self.assertIn("no human approval or grant fallback", guidance)
                self.assertIn("only where policy permits", guidance)
                self.assertIn(
                    "never\ngrants extra rounds or bypasses stop rules", guidance
                )
        self.assertIn(
            "Historical `terminal` authorization audit records remain readable",
            orchestration,
        )
        self.assertIn("outside automated\nacceptance", orchestration)
        self.assertIn("recover --source user --task-dir", orchestration)

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

    def assert_references(self, text: str, root: Path, where: str) -> int:
        """Every backticked path:line names an existing repository file and line."""
        found = 0
        for path, line in REFERENCE.findall(text):
            found += 1
            message = f"{where}: bad reference {path}:{line}"
            self.assertNotIn("\\", path, message)
            self.assertFalse(path.startswith(("/", "../", "./")), message)
            self.assertIsNone(re.match(r"^[A-Za-z]:", path), message)
            self.assertFalse(path == ".work" or path.startswith(".work/"), message)
            target = root / path
            self.assertTrue(target.is_file(), message)
            count = len(target.read_text(encoding="utf-8").splitlines())
            self.assertTrue(1 <= int(line) <= count, message)
        return found

    def assert_dead_ends_audit(self, path: Path, root: Path = ROOT) -> None:
        import epics
        import role_contracts

        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertEqual(lines[0], "# task-flow dead ends and blocking paths")
        preamble = text.split("\n## ", 1)[0]
        self.assertRegex(
            preamble, r"(?m)^Audited commit: [0-9a-f]{7,40}$", "missing audited commit"
        )
        sections = markdown_sections(text, "##")
        self.assertEqual([name for name, _ in sections], DEAD_END_SECTIONS)
        body = dict(sections)
        for marker in role_contracts.TRUNCATION_MARKERS:
            self.assertIsNone(marker.search(text), "truncation marker in document")
        self.assert_references(text, root, path.name)

        entries = markdown_sections("\n".join(body["Dead ends"]), "###")
        self.assertTrue(entries, "no dead-end entries")
        incidents: set[int] = set()
        none_entries = 0
        follow_ups: dict[str, str] = {}
        for number, (heading, entry) in enumerate(entries, 1):
            name = f"DE{number:02d}"
            self.assertRegex(heading, rf"^{name}: \S", f"entry {number} numbering")
            fields = [line for line in entry if line.strip()]
            self.assertEqual(
                [re.sub(r":.*", "", line) for line in fields],
                [f"- {field}" for field in DEAD_END_FIELDS],
                f"{name}: fields",
            )
            values = {}
            for field_name, line in zip(DEAD_END_FIELDS, fields):
                value = line.split(":", 1)[1].strip()
                self.assertTrue(value, f"{name}: empty {field_name}")
                values[field_name] = value
            incident = values["Incident"]
            if incident == "none":
                none_entries += 1
            else:
                numbers = [part.strip() for part in incident.split(",")]
                for part in numbers:
                    self.assertRegex(part, r"^\d+$", f"{name}: incident {part}")
                    self.assertIn(int(part), range(1, 14), f"{name}: incident {part}")
                    incidents.add(int(part))
            self.assertGreater(
                self.assert_references(values["Enforced at"], root, name),
                0,
                f"{name}: Enforced at needs a path:line reference",
            )
            self.assertRegex(
                values["Automatic run"], r"^(?:yes|no|partly) — \S", f"{name}: run"
            )
            follow_ups[name] = values["Follow-up"]
        self.assertEqual(incidents, set(range(1, 14)), "incidents must cover 1..13")
        self.assertGreaterEqual(none_entries, 3, "too few Incident: none entries")

        hosting = body["Self-hosting"]
        rows = table_rows(hosting)
        self.assertTrue(rows, "missing self-hosting table")
        self.assertEqual(rows[0], ["Phase", "Governing copy", "Evidence"])
        for phase in SELF_HOSTING_PHASES:
            matching = [row for row in rows[1:] if row[0].split()[:1] == [phase]]
            self.assertTrue(matching, f"self-hosting: missing phase {phase}")
            for row in matching:
                self.assertEqual(len(row), 3, f"self-hosting: {phase} row")
                self.assertTrue(
                    any(copy in row[1] for copy in GOVERNING_COPIES),
                    f"self-hosting: {phase} names no governing copy",
                )
                self.assertGreater(
                    self.assert_references(row[2], root, phase),
                    0,
                    f"self-hosting: {phase} has no evidence",
                )
        after = "\n".join(hosting).rsplit("\n|", 1)[-1]
        self.assertIn("bootstrap", after.lower(), "missing bootstrap analysis")

        epic = body["Follow-up epic"]
        table = [line for line in epic if line.startswith("|")]
        start = epic.index(table[0]) if table else 0
        block = []
        for line in epic[start:]:
            if not line.startswith("|"):
                break
            block.append(line)
        tasks = epics._parse_tasks(block, "dead-ends")
        self.assertTrue(tasks, "empty follow-up epic")
        ids = [task.task_id for task in tasks]
        described = {
            heading: [line for line in section if line.strip()]
            for heading, section in markdown_sections("\n".join(epic), "###")
        }
        for task_id in ids:
            self.assertTrue(described.get(task_id), f"no description for {task_id}")
        named: set[str] = set()
        for name, follow_up in follow_ups.items():
            if follow_up.startswith("none"):
                self.assertRegex(follow_up, r"^none — \S", f"{name}: none needs reason")
                continue
            self.assertIn(follow_up, ids, f"{name}: unknown follow-up {follow_up}")
            named.add(follow_up)
        self.assertEqual(named, set(ids), "every epic row needs a dead-end entry")

    def test_dead_ends_audit_document(self) -> None:
        self.assert_dead_ends_audit(DEAD_ENDS)

    def write_variant(self, folder: str, text: str) -> Path:
        path = Path(folder) / "task-flow-dead-ends.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_dead_ends_audit_rejects_missing_incident(self) -> None:
        original = DEAD_ENDS.read_text(encoding="utf-8")
        self.assertIn("- Incident: 13\n", original)
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_variant(
                folder, original.replace("- Incident: 13\n", "- Incident: none\n")
            )
            with self.assertRaisesRegex(AssertionError, "incidents must cover"):
                self.assert_dead_ends_audit(path)

    def test_dead_ends_audit_rejects_bad_reference(self) -> None:
        original = DEAD_ENDS.read_text(encoding="utf-8")
        reference = "`plugins/task-flow/scripts/epics.py:931`"
        self.assertIn(reference, original)
        for bad in (
            "`plugins/task-flow/scripts/epics.py:999999`",
            "`plugins/task-flow/scripts/no_such_file.py:1`",
            "`.work/task-flow-dead-ends-audit/plan.md:1`",
        ):
            with self.subTest(reference=bad), tempfile.TemporaryDirectory() as folder:
                path = self.write_variant(folder, original.replace(reference, bad))
                with self.assertRaisesRegex(AssertionError, "bad reference"):
                    self.assert_dead_ends_audit(path)

    def test_dead_ends_audit_rejects_unknown_follow_up(self) -> None:
        original = DEAD_ENDS.read_text(encoding="utf-8")
        line = "- Follow-up: epic-chained-remove\n"
        self.assertIn(line, original)
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_variant(
                folder, original.replace(line, "- Follow-up: no-such-task\n")
            )
            with self.assertRaisesRegex(AssertionError, "unknown follow-up"):
                self.assert_dead_ends_audit(path)

    def test_readme_describes_plugins_and_status_labelled_roadmap(self) -> None:
        self.assert_readme(ROOT)

    def test_usage_guide_is_bounded_and_covers_the_task_loop(self) -> None:
        self.assert_guide(ROOT)

    def test_local_markdown_documentation_links_resolve(self) -> None:
        paths = [
            ROOT / "README.md",
            ROOT / "GETTING-STARTED.md",
            ROOT / "docs/how-to.md",
            ROOT / "docs/task-flow-dead-ends.md",
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
