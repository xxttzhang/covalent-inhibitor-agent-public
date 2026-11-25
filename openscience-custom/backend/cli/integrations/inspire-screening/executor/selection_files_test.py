"""Actual Parquet/file-chain tests; fixtures are not scientific or GPU evidence."""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import worker
from refinement import select
from refinement_test import policy, rows, DIRECTIONS


class SelectionFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='selection-files-test-')
        self.root = Path(self.temp.name).resolve()
        self.original = {name: getattr(worker, name) for name in ('ROOT', 'PROJECT', 'SOURCE', 'CARSI', 'TARGET', 'PDB', 'REFERENCE')}
        worker.ROOT = self.root / 'executor'
        worker.PROJECT = self.root / 'project'
        worker.CARSI = self.root / 'carsidock'
        worker.SOURCE = self.root / 'virtual.csv'
        worker.TARGET = self.root / 'target.yaml'
        worker.PDB = self.root / 'receptor.pdb'
        worker.REFERENCE = self.root / 'reference.sdf'
        # These are hashed placeholders for file-chain tests, never engine inputs.
        for name in (worker.SOURCE, worker.REFERENCE, worker.PROJECT / 'environment/activate_carsidock.sh',
                     worker.CARSI / 'checkpoints/carsidock_230731.ckpt', worker.CARSI / 'run_carsidockcov_screening.py',
                     self.root / 'rtms.ckpt'):
            name.parent.mkdir(parents=True, exist_ok=True)
            name.write_text('non-scientific fixture')
        (worker.PROJECT / 'configs').mkdir()
        (worker.PROJECT / 'configs/local.inspire.yaml').write_text('carsidock:\n  rtmscore_checkpoint: ' + str(self.root / 'rtms.ckpt') + '\n')
        worker.TARGET.write_text('target_id: EGFR\nresidue_number: 797\nwarhead_class: acrylamide\nreaction_type: Michael Addition\nstructure:\n  carsidock_covres: A:CYS:773\n')
        worker.PDB.write_text('ATOM      1  SG  CYS A 773       0.000   0.000   0.000\n')
        snapshot = worker.snapshot()
        self.task = 'screen-' + 'a' * 24
        self.plan = {'version': 'screening-executor-v1', 'id': self.task, 'target': 'EGFR', 'site': 'C797',
                     'directory': str(self.root), 'session': 'fixture-session',
                     'library': 'CovInDB/Virtual_Screening_Library.csv', 'scope': 'bounded', 'max_records': 12, 'shortlist': 12,
                     'gates': {'min_carsidock_success': 0.75, 'min_rtmscore_coverage': 0.95},
                     'steps': [{'phase': phase, 'minutes': 1, 'max_candidates': 12} for phase in worker.PHASES],
                     'assets': snapshot['assets'], 'runtime': snapshot['runtime']}
        self.expected = worker.digest(self.plan)
        self.base = worker.directory(self.task)
        self.base.mkdir(parents=True)
        worker.write(self.base / 'plan.json', self.plan)
        data = rows()
        data[0]['rtmscore'] = -10.0
        self.frame = pd.DataFrame([{**row, 'standardized_smiles': 'CC'} for row in data])
        parent = None
        for phase in worker.PHASES:
            dest = self.base / phase
            dest.mkdir()
            name = 'candidates.parquet' if phase == 'report' else 'scores.parquet'
            self.frame.assign(score_direction='higher_is_better').to_parquet(dest / name, index=False)
            if phase == 'report':
                self.frame.to_parquet(dest / name, index=False)
            manifest = {'task_id': self.task, 'plan_digest': self.expected, 'phase': phase, 'status': 'SUCCEEDED',
                        'parent_digest': worker.digest(parent) if parent else None,
                        'artifacts': {name: worker.sha(dest / name)}, 'gates': {'fixture': True}}
            worker.write(dest / 'manifest.json', manifest)
            parent = manifest
        self.binding = {'task_id': self.task, 'source_plan_digest': self.expected,
                        'source_manifest_digest': worker.digest(parent),
                        'source_table_sha256': worker.sha(self.base / 'report/candidates.parquet'),
                        'algorithm_sha256': worker.sha(Path(worker.__file__).with_name('refinement.py')),
                        'directions': DIRECTIONS, 'policy': policy()}
        self.selection = worker.digest(self.binding)
        self.dest = self.base / 'selections' / self.selection
        self.dest.mkdir(parents=True)
        (self.dest / 'selection.lock').touch()
        self.result = select(self.frame.to_dict(orient='records'), policy(), DIRECTIONS)
        chosen = self.frame.set_index('input_sdf_index', drop=False).loc[
            [row['input_sdf_index'] for row in self.result['selected']]].reset_index(drop=True)
        chosen.to_parquet(self.dest / 'candidates.parquet', index=False)
        worker.write(self.dest / 'selection.json', self.result)
        self.manifest = {'selection_id': self.selection, 'binding': self.binding,
                         'selected_identity_digest': worker.identities(chosen), 'artifacts': {}}
        self.update_manifest()

    def tearDown(self):
        for key, value in self.original.items():
            setattr(worker, key, value)
        self.temp.cleanup()

    def update_manifest(self):
        self.manifest['artifacts'] = {name: worker.sha(self.dest / name) for name in ('selection.json', 'candidates.parquet')}
        worker.write(self.dest / 'manifest.json', self.manifest)
        return worker.digest(self.manifest)

    def read(self):
        return worker.read_selection(self.task, self.expected, self.selection, worker.digest(self.manifest))

    def test_actual_full_source_chain_and_read_only_selection(self):
        before = {str(path): (path.stat().st_mtime_ns, worker.sha(path)) for path in self.base.rglob('*') if path.is_file()}
        result = self.read()
        after = {str(path): (path.stat().st_mtime_ns, worker.sha(path)) for path in self.base.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(result['selection_verified'])
        self.assertEqual(result['input']['candidates'], self.result['selected'])
        self.assertEqual(result['selected_count'], 3)
        self.assertFalse(result['gpu_submitted'])
        self.assertTrue(result['refinement_approval_required'])
        for hidden in ('standardized_smiles', 'scaffold_smiles', 'audit', 'carsiscore'):
            self.assertNotIn('"' + hidden + '"', json.dumps(result))

    def test_creation_and_reopen_both_use_actual_recomputed_evidence(self):
        import base64
        packet = base64.b64encode(json.dumps(policy()).encode()).decode()
        first = worker.select_refinement(self.task, self.expected, packet)
        self.assertEqual(first, self.read())
        # A new policy creates a separate content-addressed selection, no overwrite.
        other = worker.select_refinement(self.task, self.expected, base64.b64encode(json.dumps(policy(consensus_count=0)).encode()).decode())
        self.assertNotEqual(first['selection_id'], other['selection_id'])
        self.assertEqual(first, self.read())

    def test_zero_effective_cap_retains_an_empty_verified_subset_without_fallback(self):
        import base64
        packet = base64.b64encode(json.dumps(policy(max_fraction=0.01)).encode()).decode()
        result = worker.select_refinement(self.task, self.expected, packet)
        self.assertEqual(result['status'], 'NO_CANDIDATES_MEET_POLICY')
        self.assertEqual(result['effective_cap'], 0)
        self.assertEqual(result['selected_count'], 0)
        self.assertEqual(result['input']['candidates'], [])
        self.assertEqual(result['input']['identity_sha256'], worker.digest([]))
        self.assertFalse(result['gpu_submitted'])
        self.assertTrue(result['refinement_approval_required'])
        self.assertEqual(worker.read_selection(self.task, self.expected, result['selection_id'],
                                              result['selection_manifest_digest']), result)
    def test_changed_bytes_and_missing_files_are_not_repaired(self):
        (self.dest / 'selection.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.read()
        (self.dest / 'candidates.parquet').unlink()
        with self.assertRaises(FileNotFoundError):
            self.read()
        self.assertFalse((self.dest / 'candidates.parquet').exists())

    def test_forged_reasons_fail_even_with_updated_file_checksum(self):
        self.result['selected'][0]['selection_channel'] = 'consensus_sentinel'
        worker.write(self.dest / 'selection.json', self.result)
        self.update_manifest()
        with self.assertRaisesRegex(ValueError, 'recomputed core'):
            self.read()

    def test_molecular_contents_cannot_change_while_identity_is_preserved(self):
        frame = pd.read_parquet(self.dest / 'candidates.parquet')
        frame.loc[0, 'standardized_smiles'] = 'CCC'
        frame.to_parquet(self.dest / 'candidates.parquet', index=False)
        self.update_manifest()
        with self.assertRaisesRegex(ValueError, 'exact core subset'):
            self.read()

    def test_source_artifact_and_approved_assets_are_rechecked(self):
        artifact = self.base / 'prepare/scores.parquet'
        contents = artifact.read_bytes()
        artifact.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.read()
        artifact.write_bytes(contents)
        worker.SOURCE.write_text('changed approved source')
        with self.assertRaisesRegex(ValueError, 'after approval'):
            self.read()

    def test_changed_frozen_manifest_is_rejected_before_following_it(self):
        expected = worker.digest(self.manifest)
        self.manifest['binding']['source_table_sha256'] = '0' * 64
        worker.write(self.dest / 'manifest.json', self.manifest)
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            worker.read_selection(self.task, self.expected, self.selection, expected)

    def test_escaping_files_and_extra_inventory_fail_closed(self):
        outside = self.root / 'outside.json'
        outside.write_bytes((self.dest / 'manifest.json').read_bytes())
        (self.dest / 'manifest.json').unlink()
        (self.dest / 'manifest.json').symlink_to(outside)
        with self.assertRaises(ValueError):
            self.read()
        (self.dest / 'manifest.json').unlink()
        self.manifest['artifacts']['../outside.json'] = worker.sha(outside)
        worker.write(self.dest / 'manifest.json', self.manifest)
        with self.assertRaisesRegex(ValueError, 'inventory'):
            self.read()


if __name__ == '__main__':
    unittest.main()
