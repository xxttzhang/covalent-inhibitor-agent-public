"""Actual isolated file-reader tests; no scientific engines or remote jobs."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import refinement_failure


class FailureFilesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='refinement-failure-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.plan = {'id': 'refine-' + 'a' * 24, 'parent_id': 'screen-' + 'b' * 24,
                     'engine': {'name': 'af3'}, 'input': {'candidates': [{'compound_id': 'fixture-original-id'}]}}
        checksum = hashlib.sha256(refinement_failure.canonical(self.plan).encode()).hexdigest()
        contents = (refinement_failure.canonical({'plan': self.plan, 'digest': checksum}) + '\n').encode()
        self.packet = {'root': str(self.root), 'parent': self.plan['parent_id'], 'task': self.plan['id'],
                       'engine': 'af3', 'digest': checksum, 'plan_sha256': hashlib.sha256(contents).hexdigest()}
        self.directory = self.root / 'tasks' / self.plan['parent_id'] / 'refinements' / self.plan['id']
        self.directory.mkdir(parents=True)
        (self.directory / 'plan.json').write_bytes(contents)

    def run_reader(self, packet=None):
        return subprocess.run([sys.executable, '-I', '-B', str(Path(refinement_failure.__file__)),
                               base64.b64encode(json.dumps(packet or self.packet).encode()).decode()],
                              capture_output=True, text=True, timeout=10)

    def test_partial_outputs_startup_failure_and_cache_are_explicit(self):
        output = self.directory / 'af3'
        (output / 'predictions').mkdir(parents=True)
        (output / 'predictions/engine.log').write_text('FIXTURE_SECRET_MUST_NOT_APPEAR_IN_TOOL_OUTPUT')
        (output / 'jax_cache').mkdir()
        (output / 'jax_cache/cache.bin').write_bytes(b'cache-not-scientific-evidence')
        (self.directory / 'runtime-admission.json').write_text('{"state":"FAILED"}')
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('FIXTURE_SECRET', result.stdout)
        value = json.loads(result.stdout)
        self.assertEqual([item['name'] for item in value['files']], ['af3/predictions/engine.log', 'plan.json', 'runtime-admission.json'])
        self.assertEqual(value['excluded_caches'], ['af3/jax_cache'])
        self.assertEqual(value['missing'], ['execution.log'])
        self.assertFalse(value['scientific_quality_verified'])
        self.assertFalse(value['compute_restarted'])
        for item in value['files']:
            contents = (self.directory / item['name']).read_bytes()
            self.assertEqual(item['bytes'], len(contents))
            self.assertEqual(item['sha256'], hashlib.sha256(contents).hexdigest())

    def test_no_engine_start_is_not_a_completed_engine(self):
        value = json.loads(self.run_reader().stdout)
        self.assertEqual(value['missing'], ['runtime-admission.json', 'execution.log', 'af3/'])
        self.assertEqual(len(value['files']), 1)
        self.assertFalse(value['scientific_quality_verified'])

    def test_wrong_identity_or_changed_plan_fails_without_leaking_contents(self):
        for key, value in [('digest', 'c' * 64), ('plan_sha256', 'c' * 64), ('engine', 'covdock'), ('task', '../secret')]:
            packet = copy.deepcopy(self.packet)
            packet[key] = value
            result = self.run_reader(packet)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {'error': 'FAILURE_INVENTORY_UNAVAILABLE'})
            self.assertEqual(result.stderr, '')
        (self.directory / 'plan.json').write_text('secret corrupted fixture')
        self.assertNotEqual(self.run_reader().returncode, 0)

    def test_symlinks_oversize_and_nonregular_outputs_fail_closed(self):
        output = self.directory / 'af3'
        output.mkdir()
        secret = self.root / 'fixture-secret'
        secret.write_text('NOT_FOR_THE_MODEL')
        linked = output / 'engine.log'
        linked.symlink_to(secret)
        self.assertNotEqual(self.run_reader().returncode, 0)
        linked.unlink()
        (output / 'linked-dir').symlink_to(self.root, target_is_directory=True)
        self.assertNotEqual(self.run_reader().returncode, 0)
        (output / 'linked-dir').unlink()
        with linked.open('wb') as stream:
            stream.truncate(128 * 1024 ** 2 + 1)
        self.assertNotEqual(self.run_reader().returncode, 0)


if __name__ == '__main__':
    unittest.main()
