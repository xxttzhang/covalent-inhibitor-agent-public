"""Guard the minimal platform's default entry configuration."""
import json
import unittest
from pathlib import Path


class PlatformEntryTests(unittest.TestCase):
    def test_default_entry_is_primary_and_delivers_backend_report(self):
        root = (Path(__file__).resolve().parents[3] / "science")
        config = json.loads((root / "ops/openscience_ligand_validation.json").read_text())
        agent = config["default_agent"]
        text = (root / ".openscience/agent" / (agent + ".md")).read_text()
        frontmatter = text.split("---", 2)[1]
        self.assertIn("mode: primary", frontmatter)
        self.assertNotIn("mode: subagent", frontmatter)
        self.assertIn("report_markdown", text)
        self.assertIn("not your chat workspace", text)
        self.assertIn("2D chemical structures", text)
        self.assertIn('Never describe Gate B2 as "all gates passed"', text)
        self.assertIn("explicitly report that missing evidence", text)


if __name__ == "__main__":
    unittest.main()
