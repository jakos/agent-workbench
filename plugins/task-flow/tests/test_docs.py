# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Check the minimal README and links after retiring the old documentation."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class ReadmeTest(unittest.TestCase):
    def test_readme_is_basic_and_has_no_dates(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 35)
        self.assertIsNone(
            re.search(r"\b(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b", text)
        )
        self.assertNotIn("<!--", text)
        self.assertIn("## Install", text)

    def test_retired_documentation_is_absent(self):
        self.assertFalse((ROOT / "docs").exists())

    def test_markdown_has_no_links_to_retired_docs(self):
        paths = [ROOT / "README.md", ROOT / "GETTING-STARTED.md"]
        paths.extend((ROOT / "plugins").rglob("*.md"))
        paths.extend((ROOT / ".claude").rglob("*.md"))
        for path in paths:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"!?\[[^]\n]+\]\(([^)\n]+)\)", text):
                if re.match(r"[a-z]+:", target):
                    continue
                self.assertIsNone(
                    re.match(r"(?:\.?\.?/)*docs(?:/|$)", target),
                    f"{path}: {target}",
                )


if __name__ == "__main__":
    unittest.main()
