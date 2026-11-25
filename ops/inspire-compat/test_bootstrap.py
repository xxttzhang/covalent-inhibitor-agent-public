"""Validate the exact installed baseline; argument is its original auth.py."""
import ast
import hashlib
from pathlib import Path
import sys
from typing import Any
import unittest
from install import bootstrap, BOOTSTRAP
from inspire.platform.web.session.envelope import _v2_result

original = Path(sys.argv.pop(1)).read_bytes()
assert hashlib.sha256(original).hexdigest() == BOOTSTRAP
before = ast.parse(original)
after = ast.parse(bootstrap(original.decode()))


class Bootstrap(unittest.TestCase):
    def test_only_workspace_discovery_changes(self):
        def unrelated(tree):
            allowed = {"USER_ROUTES_PATH", "BOOTSTRAP_ROUTES_BODY"}
            return [ast.dump(n) for n in tree.body
                    if not (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in allowed for t in n.targets))
                    and not (isinstance(n, ast.FunctionDef) and n.name == "_workspace_routes_from_payload")]
        self.assertEqual(unrelated(before), unrelated(after))

    def test_real_shared_parser_preserves_policies_and_rejects_unknown(self):
        function = next(n for n in after.body if isinstance(n, ast.FunctionDef)
                        and n.name == "_workspace_routes_from_payload")
        namespace = {"Any": Any, "_v2_result": _v2_result}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "bootstrap-parser", "exec"), namespace)
        parse = namespace[function.name]
        row = {"workspace_id": "ws-12345678-1234-1234-1234-123456789abc", "name": "test", "is_fair_workspace": False}
        ids, names, policies = parse({"Result": {"items": [row]}})
        self.assertEqual(ids, [row["workspace_id"]])
        self.assertEqual(names, {ids[0]: "test"})
        self.assertIs(policies[ids[0]], False)
        from inspire.platform.web.browser_api.workspaces import WorkspaceCapabilityError
        with self.assertRaises(WorkspaceCapabilityError):
            parse({"Result": {"items": [{**row, "is_fair_workspace": None}]}})


if __name__ == "__main__":
    unittest.main()
