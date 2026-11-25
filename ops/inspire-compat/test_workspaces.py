"""Contract tests using the actual compatibility parser; no network or compute."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "inspire.platform.web.browser_api.acceptance_workspaces", Path(__file__).with_name("workspaces.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Contract(unittest.TestCase):
    def entry(self, fair=False):
        return {"workspace_id": "ws-12345678-1234-1234-1234-123456789abc",
                "name": "test workspace", "is_fair_workspace": fair}

    def test_explicit_policies(self):
        for fair in (True, False):
            row = self.entry(fair)
            parsed = module._accessible_workspace_entries({"items": [row]})
            self.assertIs(parsed[row["workspace_id"]]["is_fair_workspace"], fair)

    def test_missing_or_nonboolean_policy_is_not_false(self):
        for value in (None, 0, 1, "false", "true", {}, []):
            with self.subTest(value=value), self.assertRaises(module.WorkspaceCapabilityError):
                module._accessible_workspace_entries({"items": [self.entry(value)]})

    def test_invalid_envelope_or_entry_fails(self):
        for value in ({}, {"routes": []}, {"items": None}, {"items": {}}, {"items": [None]}):
            with self.subTest(value=value), self.assertRaises(module.WorkspaceCapabilityError):
                module._accessible_workspace_entries(value)

    def test_duplicate_or_invalid_identity_fails(self):
        row = self.entry()
        with self.assertRaises(module.WorkspaceCapabilityError):
            module._accessible_workspace_entries({"items": [row, row]})
        for change in ({"workspace_id": "default"}, {"workspace_id": row["workspace_id"] + "\n"},
                       {"workspace_id": module.DEFAULT_WORKSPACE_ID}, {"name": ""}, {"name": None}):
            with self.subTest(change=change), self.assertRaises(module.WorkspaceCapabilityError):
                module._accessible_workspace_entries({"items": [{**row, **change}]})

    def test_explicit_empty_list_is_empty(self):
        self.assertEqual(module._accessible_workspace_entries({"items": []}), {})


if __name__ == "__main__":
    unittest.main()
