"""Actual isolated startup guard against frozen local files and harmless wrappers.

No scientific engine, licensed executable, network or compute platform is used.
"""
import base64
import copy
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

import code_release
import execution_bootstrap
import engine_startup
import refinement_stage
import refinement_stage_test
import worker


class ExecutionBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.source = refinement_stage_test.RefinementStageTest()
        self.source.setUp()
        self.addCleanup(self.source.tearDown)
        self.root = self.source.root.resolve()
        project = self.source.source.root / 'startup-fixture-science'
        shutil.copytree(Path(self.source.release['root']) / 'science', project)
        self.marker = self.root / 'activation-ran'
        activation = project / 'environment/activate_carsidock.sh'
        activation.chmod(0o644)
        activation.write_text('touch "' + str(self.marker) + '"\n'
                              'export OMP_NUM_THREADS=99 OPENBLAS_NUM_THREADS=99 MKL_NUM_THREADS=99\n')
        for name in ('run_af3_registered_batch.py', 'run_covdock_lo_registered.py'):
            entry = project / 'scripts' / name
            entry.chmod(0o644)
            entry.write_text('import json,os,sys\nprint(json.dumps({"fixture_only":True,"argv":sys.argv[1:],'
                             '"threads":[os.environ[key] for key in ("OMP_NUM_THREADS","OPENBLAS_NUM_THREADS","MKL_NUM_THREADS")],'
                             '"bash_env":os.environ.get("BASH_ENV")}))\n')
        package = code_release.build(project, self.source.source.root)
        self.release = code_release.install(package['archive'], self.root / 'releases', package['release_id'], package['manifest_sha256'])
        self.plan = copy.deepcopy(self.source.plan)
        self.plan.update(code={key: self.release[key] for key in ('release_id', 'manifest_sha256')},
                         runtime={'name': 'af3', 'python': sys.executable},
                         resource={'quota': '1,2,16'}, finish_before=int(time.time() * 1000) + 60000)
        self.plan['assets'] = {str(Path(self.release['root']) / name): checksum for name, checksum in self.release['files'].items()}
        self.plan['assets'][sys.executable] = execution_bootstrap.checksum(Path(sys.executable))
        self.expected = worker.digest(self.plan)
        self.staged = refinement_stage.stage(self.root, self.plan, self.expected)
        argv = [sys.executable, '-B', str(Path(self.release['root']) / 'science/scripts/run_af3_registered_batch.py'),
                '--candidate-table', self.plan['input']['path'], '--expected-input-sha256', self.plan['input']['sha256'],
                '--expected-identity-sha256', self.plan['input']['identity_sha256'],
                '--expected-input-count', str(len(self.plan['input']['candidates'])), '--output-dir', self.staged['output']]
        self.packet = {'root': str(self.root), 'release': self.release['release_id'], 'manifest': self.release['manifest_sha256'],
                       'checker_sha256': self.release['files']['code_release.py'], 'plan_sha256': self.staged['plan_sha256'],
                       'parent': self.plan['parent_id'], 'task': self.plan['id'], 'argv': argv}

    def run_guard(self, packet=None, env=None):
        return subprocess.run([sys.executable, '-I', '-B', '-c', Path(engine_startup.__file__).read_text() + '\n' + Path(execution_bootstrap.__file__).read_text(),
                               base64.b64encode(json.dumps(packet or self.packet).encode()).decode()],
                              capture_output=True, text=True, timeout=30, env=env)

    def test_opted_in_startup_failure_prevents_activation_and_retains_first_failure(self):
        plan = copy.deepcopy(self.plan)
        plan['id'] = 'refine-' + 'd' * 24
        plan['runtime_admission'] = 'cpu-before-approval-gpu-at-start-v1'
        plan['runtime']['af3_python'] = '/nonexistent-fixture-interpreter'
        staged = refinement_stage.stage(self.root, plan, worker.digest(plan))
        packet = copy.deepcopy(self.packet)
        packet.update(task=plan['id'], plan_sha256=staged['plan_sha256'])
        packet['argv'][packet['argv'].index('--output-dir') + 1] = staged['output']
        result = self.run_guard(packet)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())
        receipt = Path(staged['path']).parent / 'runtime-admission.json'
        first = receipt.read_bytes()
        value = json.loads(first)
        self.assertEqual(value['state'], 'FAILED')
        self.assertFalse(value['scientific_inference_completed'])
        self.assertNotEqual(self.run_guard(packet).returncode, 0)
        self.assertEqual(receipt.read_bytes(), first)
        self.assertFalse(self.marker.exists())

    def test_real_guard_launches_only_frozen_fixture_and_restores_approved_threads(self):
        malicious = self.root / 'inherited.sh'
        unexpected = self.root / 'inherited-executed'
        malicious.write_text('touch "' + str(unexpected) + '"\n')
        result = self.run_guard(env={**os.environ, 'BASH_ENV': str(malicious), 'ENV': str(malicious)})
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        data = json.loads(result.stdout)
        self.assertTrue(data['fixture_only'])
        self.assertEqual(data['argv'], self.packet['argv'][3:])
        self.assertEqual(data['threads'], ['2', '2', '2'])
        self.assertIsNone(data['bash_env'])
        self.assertTrue(self.marker.exists())
        self.assertFalse(unexpected.exists())
        self.assertFalse(Path(self.staged['output']).exists())

    def test_changed_runtime_input_plan_and_activation_prevent_any_wrapper_start(self):
        paths = [Path(self.plan['input']['path']), Path(self.staged['path']),
                 Path(self.release['root']) / 'science/environment/activate_carsidock.sh']
        for path in paths:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.chmod(0o644)
                path.write_bytes(b'changed after queue submission')
                result = self.run_guard()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.marker.exists())
                self.assertEqual(result.stdout, '')
                path.write_bytes(original)

    def test_covdock_vector_uses_its_own_frozen_entry_without_a_license_checkout(self):
        plan = copy.deepcopy(self.plan)
        plan['id'] = 'refine-' + 'b' * 24
        plan['engine']['name'] = plan['runtime']['name'] = 'covdock'
        plan['resource']['quota'] = '0,2,16'
        staged = refinement_stage.stage(self.root, plan, worker.digest(plan))
        packet = copy.deepcopy(self.packet)
        packet.update(task=plan['id'], plan_sha256=staged['plan_sha256'])
        packet['argv'][2] = str(Path(self.release['root']) / 'science/scripts/run_covdock_lo_registered.py')
        packet['argv'][packet['argv'].index('--output-dir') + 1] = staged['output']
        result = self.run_guard(packet)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertTrue(json.loads(result.stdout)['fixture_only'])
        self.assertFalse(Path(staged['output']).exists())

    def test_expired_task_and_substituted_arguments_are_rejected(self):
        for flag, value in (('--candidate-table', '/wrong/input'), ('--expected-input-count', '100'),
                            ('--output-dir', '/wrong/output')):
            packet = copy.deepcopy(self.packet)
            packet['argv'][packet['argv'].index(flag) + 1] = value
            result = self.run_guard(packet)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.marker.exists())
        packet = copy.deepcopy(self.packet)
        packet['argv'] += ['--expected-input-count', '2']
        self.assertNotEqual(self.run_guard(packet).returncode, 0)
        path = Path(self.staged['path'])
        saved = json.loads(path.read_bytes())
        saved['plan']['finish_before'] = 1
        saved['digest'] = worker.digest(saved['plan'])
        path.chmod(0o644)
        path.write_text(worker.canonical(saved) + '\n')
        result = self.run_guard({**self.packet, 'plan_sha256': execution_bootstrap.checksum(path)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('deadline elapsed', result.stderr)
        self.assertFalse(self.marker.exists())

    def test_changed_checker_never_executes_its_contents(self):
        path = Path(self.release['root']) / 'code_release.py'
        marker = self.root / 'untrusted-checker-executed'
        path.chmod(0o644)
        path.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        result = self.run_guard()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
