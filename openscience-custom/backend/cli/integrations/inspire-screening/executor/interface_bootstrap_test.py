"""Actual release/AST inspection through the standalone bootstrap; no engines."""
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import code_release
from interface_bootstrap import inspect_release


class InterfaceBootstrapTest(unittest.TestCase):
    def test_exact_release_both_engines_no_file_writes_and_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix='interface-bootstrap-') as directory:
            root = Path(directory).resolve()
            (root / 'archives').mkdir()
            (root / 'releases').mkdir()
            project = Path(__file__).resolve().parents[6] / 'science'
            archive = code_release.build(project, root / 'archives')
            released = code_release.install(archive['archive'], root / 'releases', archive['release_id'], archive['manifest_sha256'])
            location = Path(released['root'])
            packet = {'root': str(root), 'engine': 'af3', 'release_id': archive['release_id'],
                      'manifest_sha256': archive['manifest_sha256'], 'checker_sha256': released['files']['code_release.py']}
            before = {str(file.relative_to(location)): hashlib.sha256(file.read_bytes()).hexdigest()
                      for file in location.rglob('*') if file.is_file()}
            for engine in ('af3', 'covdock'):
                data = {**packet, 'engine': engine}
                result = inspect_release(data)
                self.assertTrue(result['passes_static_checks'])
                self.assertEqual(result['quality_policies'], ['refinement-structural-qc-v1', 'refinement-structural-qc-v2-provisional'])
                self.assertFalse(result['runtime_verified'])
                self.assertFalse(result['license_checkout_verified'])
                self.assertFalse(result['resource_approval_granted'])
                child = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).with_name('interface_bootstrap.py')),
                    base64.b64encode(json.dumps(data).encode()).decode()], capture_output=True, text=True, timeout=10)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertEqual(json.loads(child.stdout), result)
            after = {str(file.relative_to(location)): hashlib.sha256(file.read_bytes()).hexdigest()
                     for file in location.rglob('*') if file.is_file()}
            self.assertEqual(before, after)
            for change in ({'checker_sha256': '0' * 64}, {'manifest_sha256': '0' * 64}, {'engine': 'other'}, {'extra': True}):
                with self.assertRaises(ValueError):
                    inspect_release({**packet, **change})
            reader = location / 'engine_interface.py'
            reader.chmod(0o644)
            reader.write_text('raise RuntimeError("must not execute modified source")')
            with self.assertRaises(ValueError):
                inspect_release(packet)


if __name__ == '__main__':
    unittest.main()
