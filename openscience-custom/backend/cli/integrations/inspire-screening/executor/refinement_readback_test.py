"""Actual release/bootstrap/Parquet readback; structure fixtures are not chemistry."""
import base64
import json
import subprocess
import sys
import unittest
from pathlib import Path

import pandas as pd
import stage_bootstrap
import stage_bootstrap_test
import worker


class RefinementReadbackTest(unittest.TestCase):
    def setUp(self):
        self.source = stage_bootstrap_test.StageBootstrapTest()
        self.source.setUp()
        self.addCleanup(self.source.doCleanups)
        self.root = self.source.root

    def fixture(self, engine, corrupt_score=False, geometry=False, classified=False):
        plan = self.source.plan
        plan['engine']['name'] = engine
        plan['runtime']['name'] = engine
        if engine == 'af3':
            plan['runtime'].update(protein_chain='A', ligand_chain='L')
        if geometry:
            plan['geometry_contract'] = 'af3-geometry-evidence-v1'
            plan['runtime'].update(cysteine_sequence_index=105, source_residue_number=797)
        if classified:
            plan['result_contract'] = 'screening-refinement-outcome-v2'
            plan['runtime'].update(attachment_residue='A:797', schrodinger_root=str(self.root / 'missing-vendor'),
                                  license_file=str(self.root / 'missing-license'))
        packet = {**self.source.packet, 'digest': worker.digest(plan),
                  'payload': base64.b64encode(json.dumps(plan).encode()).decode()}
        staged = stage_bootstrap.execute(packet)
        self.packet = {**packet, 'action': 'outputs', 'payload': None, 'evidence_sha256': None}
        self.output = Path(staged['output'])
        self.output.mkdir()
        rows = [{'input_sdf_index': row['input_sdf_index'], 'compound_id': row['compound_id'],
                 'engine_status': 'SUCCESS', 'raw_score': float(index + 1), 'failure_reason': None,
                 'score_direction': 'lower_is_better'} for index, row in enumerate(plan['input']['candidates'])]
        contract = {'input_count': len(rows), 'input_sha256': plan['input']['sha256'],
                    'identity_sha256': plan['input']['identity_sha256'], 'verified': True, 'grants_resource_approval': False}
        if engine == 'af3':
            for row in rows:
                name = 'fixture_' + str(row['input_sdf_index'])
                folder = self.output / name
                folder.mkdir()
                model = folder / (name + '_model.cif')
                model.write_text('NON_SCIENTIFIC_STRUCTURE_FIXTURE\n')
                score = row['raw_score']
                summary = {'ranking_score': .8, 'iptm': .7, 'ptm': .6, 'has_clash': False,
                           'chain_pair_pae_min': [[1., 2.], [score, 3.]]}
                confidence = {'token_chain_ids': ['A', 'L'], 'pae': [[1., 2.], [score, 3.]],
                              'atom_chain_ids': ['A', 'L'], 'atom_plddts': [90., 70.]}
                (folder / (name + '_summary_confidences.json')).write_text(json.dumps(summary))
                (folder / (name + '_confidences.json')).write_text(json.dumps(confidence))
                row.update(ranking_score=.8, iptm=.7, ptm=.6, has_clash=0., mpae=score,
                           protein_to_ligand_pae_min=2., ligand_to_protein_pae_min=score,
                           protein_ligand_pae_mean=(2. + score) / 2, ligand_plddt_mean=70.)
                row.update(model_cif=str(model), job_name=name)
            manifest = {'inputs': {'refinement_input_contract': contract, 'candidate_table': {'sha256': contract['input_sha256']}},
                        'execution': {'resume_enabled': False, 'reparse_only': False}}
            table_name, manifest_name = 'af3_all_scores.parquet', 'stage4_selection_manifest.json'
            self.structure = Path(rows[0]['model_cif'])
        else:
            self.structure = self.output / 'combined.maegz'
            self.structure.write_bytes(b'NON_SCIENTIFIC_MULTIPOSE_FIXTURE')
            audit = self.output / 'audit.json'
            poses = [{'pose_index': i, 'title': row['compound_id'] + '__i' + str(row['input_sdf_index']),
                      'r_fixture_score': row['raw_score']} for i, row in enumerate(rows)]
            audit.write_text(json.dumps({'input': str(self.structure), 'rows': poses}))
            for i, row in enumerate(rows):
                row.update(covdock_pose_file=str(self.structure), covdock_pose_index=i, covdock_title=poses[i]['title'],
                           covdock_inspection_file=str(audit), covdock_inspection_sha256=worker.sha(audit))
            manifest = {'refinement_input_contract': contract, 'input_score_source': 'core',
                        'input_candidate_table_sha256': contract['input_sha256'], 'score_property': 'r_fixture_score',
                        'resume': {'requested': False, 'ligprep_reused': False, 'covdock_reused': False, 'source_artifact_dir': None}}
            table_name, manifest_name = 'covdock_lo_all_scores.parquet', 'covdock_lo_registered_manifest.json'
        if corrupt_score:
            rows[0]['raw_score'] = 19.5  # Internally rehashed table, inconsistent with original JSON.
        table = self.output / table_name
        pd.DataFrame(rows).to_parquet(table, index=False)
        manifest['outputs'] = {table_name: {'path': str(table), 'sha256': worker.sha(table)}}
        (self.output / manifest_name).write_text(json.dumps(manifest))
        # Freeze through the actual released collector in a fresh interpreter.
        command = ('import sys; sys.path.insert(0, sys.argv[1]); '
                   'from covscreen.refinement_output import freeze_refinement_outputs; import json; '
                   'print(json.dumps(freeze_refinement_outputs(sys.argv[2],sys.argv[3],json.loads(sys.argv[4]))))')
        frozen = subprocess.run([sys.executable, '-B', '-c', command,
            str(self.source.release / 'science/src'), str(self.output), engine, json.dumps(contract)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(frozen.returncode, 0, frozen.stderr[-2000:])
        return json.loads(frozen.stdout)['sha256']

    def test_actual_released_af3_readback_is_read_only_and_not_scientific_acceptance(self):
        witness = self.fixture('af3')
        before = {str(path): (path.stat().st_mtime_ns, worker.sha(path)) for path in self.root.rglob('*') if path.is_file()}
        first = stage_bootstrap.execute(self.packet)
        fresh = stage_bootstrap.execute({**self.packet, 'evidence_sha256': witness})
        self.assertEqual(first, fresh)
        self.assertEqual(first['manifest_sha256'], witness)
        self.assertEqual(first['digest'], self.packet['digest'])
        self.assertFalse(first['evidence']['scientific_quality_verified'])
        self.assertFalse(first['evidence']['resource_approval_granted'])
        after = {str(path): (path.stat().st_mtime_ns, worker.sha(path)) for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_covdock_readback_preserves_distinct_shared_container_entries(self):
        self.fixture('covdock')
        result = stage_bootstrap.execute(self.packet)
        refs = [row['structure'] for row in result['evidence']['candidates']]
        self.assertTrue(all(row['path'] == 'combined.maegz' for row in refs))
        self.assertEqual(len({row['pose_index'] for row in refs}), len(refs))

    def test_classified_covdock_result_cannot_pass_without_frozen_real_sdk_reader(self):
        self.fixture('covdock', classified=True)
        result = subprocess.run([sys.executable, '-B', str(self.source.release / 'refinement_readback.py'),
            'outputs', '--root', str(self.root), '--digest', self.packet['digest'],
            '--parent', self.packet['parent'], '--task', self.packet['task']],
            capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SDK reader, launcher and license must be hash-bound', result.stderr)
        with self.assertRaisesRegex(RuntimeError, 'Verified staging worker failed'):
            stage_bootstrap.execute(self.packet)

    def test_released_geometry_contract_cannot_accept_placeholder_structures_or_unregistered_chemistry(self):
        self.fixture('af3', geometry=True)
        result = subprocess.run([sys.executable, '-B', str(self.source.release / 'refinement_readback.py'),
            'outputs', '--root', str(self.root), '--digest', self.packet['digest'],
            '--parent', self.packet['parent'], '--task', self.packet['task']],
            capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Frozen molecular input lacks SMILES or declared warhead', result.stderr)
        with self.assertRaisesRegex(RuntimeError, 'Verified staging worker failed'):
            stage_bootstrap.execute(self.packet)

    def test_released_readback_rejects_wrong_score_even_with_consistent_file_hashes(self):
        self.fixture('af3', corrupt_score=True)
        # The outer bootstrap intentionally redacts child stderr. Check the
        # actual released reader's precise reason, then its public failure.
        result = subprocess.run([sys.executable, '-B', str(self.source.release / 'refinement_readback.py'),
            'outputs', '--root', str(self.root), '--digest', self.packet['digest'],
            '--parent', self.packet['parent'], '--task', self.packet['task']],
            capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('metric differs', result.stderr)
        with self.assertRaisesRegex(RuntimeError, 'Verified staging worker failed'):
            stage_bootstrap.execute(self.packet)

    def test_changed_structure_or_saved_witness_cannot_be_read_as_complete(self):
        self.fixture('af3')
        with self.assertRaises(RuntimeError):
            stage_bootstrap.execute({**self.packet, 'evidence_sha256': '0' * 64})
        self.structure.write_text('CHANGED_NON_SCIENTIFIC_FIXTURE')
        with self.assertRaises(RuntimeError):
            stage_bootstrap.execute(self.packet)

    def test_missing_artifacts_are_not_recreated_and_changed_parent_is_rejected(self):
        self.fixture('af3')
        manifest = self.output / 'refinement_artifacts.json'
        original = manifest.read_bytes()
        manifest.unlink()
        with self.assertRaises(RuntimeError):
            stage_bootstrap.execute(self.packet)
        self.assertFalse(manifest.exists())
        manifest.write_bytes(original)
        parent = self.root / 'tasks' / self.source.plan['parent_id'] / 'prepare/scores.parquet'
        parent.write_bytes(b'CHANGED_CORE_FIXTURE')
        with self.assertRaises(RuntimeError):
            stage_bootstrap.execute(self.packet)


if __name__ == '__main__':
    unittest.main()
