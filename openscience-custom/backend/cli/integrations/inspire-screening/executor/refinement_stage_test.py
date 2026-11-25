"""Actual file-chain/release staging tests; no approval or scientific execution."""
import base64
import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import code_release
import refinement_stage
import worker
import selection_files_test


class RefinementStageTest(unittest.TestCase):
    def setUp(self):
        # Reuse the actual on-disk core/selection fixture, not a mocked verifier.
        self.source = selection_files_test.SelectionFilesTest()
        self.source.setUp()
        self.root = worker.ROOT
        project = Path(__file__).resolve().parents[6] / 'science'
        package = code_release.build(project, self.source.root)
        (self.root / 'releases').mkdir()
        self.release = code_release.install(package['archive'], self.root / 'releases',
                                            package['release_id'], package['manifest_sha256'])
        selection = worker.read_selection(self.source.task, self.source.expected,
                                          self.source.selection, worker.digest(self.source.manifest))
        self.plan = {'version': 'screening-refinement-v1', 'id': 'refine-' + 'a' * 24,
            'directory': self.source.plan['directory'], 'session': self.source.plan['session'],
            'parent_id': self.source.task, 'parent_digest': self.source.expected,
            'report_digest': self.source.binding['source_manifest_digest'], 'selection_id': self.source.selection,
            'selection_manifest_digest': worker.digest(self.source.manifest), 'input': selection['input'],
            'engine': {'name': 'af3'}, 'runtime': {'name': 'af3'},
            'code': {key: self.release[key] for key in ('release_id', 'manifest_sha256')},
            'assets': {str(Path(self.release['root']) / name): checksum for name, checksum in self.release['files'].items()}}
        self.expected = worker.digest(self.plan)

    def tearDown(self):
        self.source.tearDown()

    def stage(self):
        return refinement_stage.stage(self.root, self.plan, self.expected)

    def inspect(self):
        return refinement_stage.inspect(self.root, self.plan['parent_id'], self.plan['id'], self.expected)

    def test_exact_stage_is_idempotent_and_readback_does_not_create_engine_output(self):
        result = self.stage()
        before = Path(result['path']).stat().st_mtime_ns
        self.assertEqual(self.stage(), result)
        self.assertEqual(self.inspect(), result)
        self.assertEqual(Path(result['path']).stat().st_mtime_ns, before)
        self.assertFalse(Path(result['output']).exists())
        self.assertIs(result['runtime_verified'], False)
        self.assertIs(result['resource_approval_granted'], False)
        self.assertEqual(set(json.loads(Path(result['path']).read_text())), {'plan', 'digest'})

    def test_different_plan_cannot_overwrite_existing_staging(self):
        result = self.stage()
        original = Path(result['path']).read_bytes()
        self.plan['request'] = 'different fixture task'
        self.expected = worker.digest(self.plan)
        with self.assertRaisesRegex(ValueError, 'never overwrite'):
            self.stage()
        self.assertEqual(Path(result['path']).read_bytes(), original)

    def test_classified_result_contract_is_frozen_without_engine_execution(self):
        self.plan['result_contract'] = 'screening-refinement-outcome-v2'
        self.expected = worker.digest(self.plan)
        result = self.stage()
        saved = Path(result['path']).read_bytes()
        self.assertEqual(json.loads(saved)['plan']['result_contract'], self.plan['result_contract'])
        self.assertEqual(self.inspect(), result)
        self.assertFalse(Path(result['output']).exists())
        del self.plan['result_contract']
        self.expected = worker.digest(self.plan)
        with self.assertRaisesRegex(ValueError, 'never overwrite'):
            self.stage()
        self.assertEqual(Path(result['path']).read_bytes(), saved)

    def test_unknown_or_null_result_contract_cannot_be_staged(self):
        for contract in ('screening-refinement-outcome-v1', 'unknown', None):
            with self.subTest(contract=contract):
                self.plan['result_contract'] = contract
                self.expected = worker.digest(self.plan)
                with self.assertRaisesRegex(ValueError, 'result contract'):
                    self.stage()
                self.assertFalse((self.root / 'tasks' / self.plan['parent_id'] / 'refinements' / self.plan['id'] / 'plan.json').exists())

    def test_geometry_contract_is_frozen_and_cannot_be_silently_changed(self):
        self.plan['geometry_contract'] = 'af3-geometry-evidence-v1'
        self.expected = worker.digest(self.plan)
        result = self.stage()
        self.assertEqual(json.loads(Path(result['path']).read_text())['plan']['geometry_contract'], self.plan['geometry_contract'])
        self.assertEqual(self.inspect(), result)
        self.plan['geometry_contract'] = None
        self.expected = worker.digest(self.plan)
        with self.assertRaisesRegex(ValueError, 'geometry contract'):
            self.stage()

    def test_input_identity_source_and_code_changes_prevent_initial_staging(self):
        for transform in (
            lambda value: value.update(session='another-session'),
            lambda value: value.update(parent_digest='0' * 64),
            lambda value: value.update(selection_manifest_digest='0' * 64),
            lambda value: value['input'].update(path='/outside/candidates.parquet'),
            lambda value: value['input'].update(identity_sha256='0' * 64),
            lambda value: value['input']['candidates'][0].update(compound_id='substituted'),
            lambda value: value['code'].update(manifest_sha256='0' * 64),
            lambda value: value.update(assets={}),
        ):
            plan = copy.deepcopy(self.plan)
            transform(plan)
            with self.assertRaises(ValueError):
                refinement_stage.stage(self.root, plan, worker.digest(plan))
            self.assertFalse((self.root / 'tasks' / plan['parent_id'] / 'refinements').exists())

    def test_changed_real_core_file_or_release_invalidates_readback(self):
        self.stage()
        source = self.root / 'tasks' / self.plan['parent_id'] / 'prepare/scores.parquet'
        contents = source.read_bytes()
        source.write_bytes(b'altered actual source artifact')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            self.inspect()
        source.write_bytes(contents)
        helper = Path(self.release['root']) / 'science/src/covscreen/refinement_output.py'
        helper.chmod(0o644)
        helper.write_text('altered release source')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            self.inspect()

    def test_plan_and_destination_symlinks_are_rejected(self):
        directory = self.root / 'tasks' / self.plan['parent_id'] / 'refinements'
        directory.symlink_to(self.source.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            self.stage()

    def test_read_rejects_modified_plan_and_wrong_task_identity(self):
        result = self.stage()
        file = Path(result['path'])
        file.chmod(0o644)
        packet = json.loads(file.read_text())
        packet['plan']['id'] = 'refine-' + 'b' * 24
        file.write_text(json.dumps(packet))
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.inspect()

    def test_new_release_contains_helpers_and_old_inventory_remains_valid(self):
        self.assertIn('refinement_stage.py', self.release['files'])
        self.assertIn('runtime_probe.py', self.release['files'])
        old = {name: value for name, value in self.release['files'].items() if name not in code_release.EXTRA_CONTROL}
        self.assertEqual(code_release.manifest(old)['files'], old)

    def test_actual_cli_stages_and_reads_without_engine_execution(self):
        entry = str(Path(refinement_stage.__file__).resolve())
        common = [sys.executable, '-B', entry]
        payload = base64.b64encode(json.dumps(self.plan).encode()).decode()
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        freeze = subprocess.run([*common, 'freeze', '--root', str(self.root), '--digest', self.expected,
                                 '--payload', payload], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(freeze.returncode, 0, freeze.stderr[-2000:])
        result = json.loads(freeze.stdout)
        loaded = subprocess.run([*common, 'read', '--root', str(self.root), '--digest', self.expected,
                                 '--parent', self.plan['parent_id'], '--task', self.plan['id']],
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(loaded.returncode, 0, loaded.stderr[-2000:])
        self.assertEqual(json.loads(loaded.stdout), result)
        self.assertFalse(Path(result['output']).exists())

    def test_two_real_processes_publish_only_one_identical_frozen_plan(self):
        entry = str(Path(refinement_stage.__file__).resolve())
        payload = base64.b64encode(json.dumps(self.plan).encode()).decode()
        command = [sys.executable, '-B', entry, 'freeze', '--root', str(self.root),
                   '--digest', self.expected, '--payload', payload]
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        children = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(2)]
        try:
            results = []
            for child in children:
                output, error = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, error[-2000:])
                results.append(json.loads(output))
            self.assertEqual(results[0], results[1])
            self.assertEqual(self.inspect(), results[0])
            self.assertEqual(list(Path(results[0]['path']).parent.glob('.plan-*')), [])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
