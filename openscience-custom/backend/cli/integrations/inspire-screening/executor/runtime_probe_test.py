"""Actual subprocess diagnostics; no model loading, GPU or license use."""
import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from runtime_probe import environment, import_code, inspect, metadata, run


class RuntimeProbeTest(unittest.TestCase):
    def test_cpu_environment_and_structured_imports(self):
        env = environment()
        self.assertEqual(env['CUDA_VISIBLE_DEVICES'], '')
        self.assertEqual(env['JAX_PLATFORMS'], 'cpu')
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertNotIn('SCHROD_LICENSE_FILE', env)
        self.assertNotIn('PYTHONPATH', env)
        code = import_code(('json', 'deliberately_missing_screening_dependency'))
        ast.parse(code)
        result = run([sys.executable, '-B', '-c', code], env, os.getcwd())
        self.assertFalse(result['passed'])
        self.assertTrue(result['imports']['json']['imported'])
        self.assertEqual(result['imports']['deliberately_missing_screening_dependency']['error_type'], 'ModuleNotFoundError')
        self.assertEqual(result['python'], list(sys.version_info[:3]))

    def test_real_success_failure_timeout_and_invalid_response(self):
        env = environment()
        result = run([sys.executable, '-B', '-c', import_code(('json',))], env, os.getcwd())
        self.assertTrue(result['passed'])
        for code in ('raise SystemExit(2)', 'print("not JSON")', 'print("{}")'):
            self.assertFalse(run([sys.executable, '-B', '-c', code], env, os.getcwd())['passed'])
        for data in ({'passed': True, 'imports': {'fake': {'imported': False}}},
                     {'python': [3, 12, 1], 'executable': sys.executable, 'imports': {'fake': True}}):
            code = 'print(' + repr(json.dumps(data)) + ')'
            self.assertFalse(run([sys.executable, '-B', '-c', code], env, os.getcwd())['passed'])
        result = run([sys.executable, '-B', '-c', 'import time; time.sleep(2)'], env, os.getcwd(), timeout=0.05)
        self.assertEqual(result['error_type'], 'TimeoutExpired')

    def test_license_metadata_never_exposes_contents_and_hashing_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'fixture.lic'
            file.write_text('NONSECRET_TEST_CONTENT_NOT_FOR_OUTPUT')
            result = metadata(file, True)
            self.assertIn('sha256', result)
            self.assertNotIn('NONSECRET_TEST_CONTENT_NOT_FOR_OUTPUT', json.dumps(result))
            file.write_bytes(b'x' * (1024 * 1024 + 1))
            self.assertNotIn('sha256', metadata(file, True))
            self.assertIn('hash_error', metadata(file, True))

    def test_requested_interpreter_and_complete_import_inventory_are_checked(self):
        command = [sys.executable, '-B', '-c', import_code(('json',))]
        options = {'env': environment(), 'cwd': os.getcwd()}
        self.assertTrue(run(command, **options, modules=('json',), executable=sys.executable)['passed'])
        for modules in (('json', 'not_checked'), ('different_module',)):
            result = run(command, **options, modules=modules, executable=sys.executable)
            self.assertFalse(result['passed'])
            self.assertIn('inventory', result['source_error'])
        result = run(command, **options, modules=('json',), executable='/not-the-requested-python')
        self.assertFalse(result['passed'])
        self.assertIn('Interpreter', result['source_error'])

    def test_covdock_presence_check_does_not_launch_licensed_programs_or_claim_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'environment').mkdir()
            (root / 'environment/activate_carsidock.sh').write_text('return 0\n')
            software = root / 'software'
            software.mkdir()
            sentinel = root / 'must-not-exist'
            for name in ('run', 'ligprep', 'covalent_docking'):
                file = software / name
                file.write_text('#!/bin/sh\ntouch "' + str(sentinel) + '"\n')
                file.chmod(0o755)
            license_file = root / 'fixture.lic'
            license_file.write_text('nonsecret test contents')
            result = inspect({'name': 'covdock', 'python': sys.executable, 'schrodinger_root': str(software),
                'license_file': str(license_file), 'runtime_lib_dir': str(root)}, root)
            self.assertFalse(sentinel.exists())
            self.assertTrue(result['files']['covalent_docking']['exists'])
            for field in ('ready', 'gpu_verified', 'license_checkout_verified', 'resource_approval_granted'):
                self.assertIs(result[field], False)


if __name__ == '__main__':
    unittest.main()
