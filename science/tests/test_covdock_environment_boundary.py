"""Regression for helper imports vs. the isolated vendor environment."""
import ast
import os
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def environment_before_command(script, variable, command):
    tree = ast.parse((ROOT / "scripts" / script).read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    for node in ast.walk(main):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        start = None
        for index, statement in enumerate(body):
            if isinstance(statement, ast.Assign):
                names = [t.id for t in statement.targets if isinstance(t, ast.Name)]
                if variable in names:
                    start = index
                if command in names and start is not None:
                    module = ast.Module(body=body[start:index], type_ignores=[])
                    return compile(ast.fix_missing_locations(module), str(script), "exec")
    raise AssertionError("environment boundary not found")


class EnvironmentBoundaryTest(unittest.TestCase):
    def test_persistent_scratch_is_outside_final_output_scan(self):
        tree = ast.parse((ROOT / "scripts/run_covdock_job.py").read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        start = next(i for i, n in enumerate(main.body) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "scratch" for t in n.targets))
        end = next(i for i in range(start, len(main.body)) if isinstance(main.body[i], ast.If))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "covdock_lo"
            scope = {"output_dir": output, "env": {}, "extra_options": [], "manifest": {}}
            exec(compile(ast.Module(body=main.body[start:end], type_ignores=[]), "scratch_boundary", "exec"), scope)
            scratch = Path(scope["env"]["SCHRODINGER_TMPDIR"])
            self.assertTrue(scratch.is_dir())
            self.assertNotEqual(scratch, output)
            self.assertNotIn(output, scratch.parents)
            self.assertEqual(scope["extra_options"], ["-TMPDIR", str(scratch), "-SAVE"])
            self.assertTrue(scope["manifest"]["retain_job_scratch"])

    def test_invocation_manifest_is_saved_before_vendor_wait(self):
        tree = ast.parse((ROOT / "scripts/run_covdock_job.py").read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        start = next(i for i, n in enumerate(main.body) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "stdout_path" for t in n.targets))
        end = next(i for i in range(start, len(main.body)) if isinstance(main.body[i], ast.With))
        with tempfile.TemporaryDirectory() as directory:
            from types import SimpleNamespace
            path = Path(directory) / "run_manifest.json"
            scope = {"output_dir": Path(directory), "args": SimpleNamespace(jobname="example"),
                     "manifest_path": path, "manifest": {"status": "PRECHECK_PENDING"},
                     "command": ["vendor", "-WAIT"], "json": json}
            exec(compile(ast.Module(body=main.body[start:end], type_ignores=[]), "manifest_boundary", "exec"), scope)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["status"], "STARTED")
            self.assertEqual(saved["command"], ["vendor", "-WAIT"])
            self.assertNotIn("return_code", saved)
            self.assertNotIn("parsed_outputs", saved)

    def test_project_helper_retains_runtime_without_loader_injection(self):
        from types import SimpleNamespace
        values = {key: "/registered/" + key for key in
                  ("PYTHONPATH", "PYTHONHOME", "LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT")}
        scope = {"os": os, "args": SimpleNamespace(
            schrodinger_root=Path("/vendor"), license_file=Path("/official.lic"))}
        code = environment_before_command("run_covdock_lo_registered.py", "env", "covdock_command")
        with patch.dict(os.environ, values, clear=True):
            exec(code, scope)
        for key in ("PYTHONPATH", "PYTHONHOME", "LD_LIBRARY_PATH"):
            self.assertEqual(scope["env"][key], values[key])
        for key in ("LD_PRELOAD", "LD_AUDIT"):
            self.assertNotIn(key, scope["env"])

    def test_vendor_boundary_still_strips_project_and_loader_paths(self):
        tree = ast.parse((ROOT / "scripts/run_covdock_job.py").read_text())
        forbidden = {"LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT", "PYTHONHOME", "PYTHONPATH"}
        loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)
                 and isinstance(n.iter, ast.Tuple)
                 and {e.value for e in n.iter.elts if isinstance(e, ast.Constant)} == forbidden]
        self.assertEqual(len(loops), 1)
        scope = {"env": dict.fromkeys(forbidden, "untrusted")}
        exec(compile(ast.Module(body=loops, type_ignores=[]), "vendor_boundary", "exec"), scope)
        self.assertEqual(scope["env"], {})


if __name__ == "__main__":
    unittest.main()
