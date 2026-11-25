"""Actual file bindings and CSV proofs; synthetic inputs never run an engine."""
import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

import worker


class ResearchInputsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='research-input-files-')
        self.root = Path(self.temp.name).resolve()
        names = ('ROOT', 'PROJECT', 'SOURCE', 'CARSI', 'TARGET', 'PDB', 'REFERENCE', 'RELEASE', 'RESEARCH_INPUT_ROOT')
        self.original = {name: getattr(worker, name) for name in names}
        worker.ROOT = self.root / 'executor'
        worker.PROJECT = self.root / 'project'
        worker.CARSI = self.root / 'carsidock'
        worker.SOURCE = self.root / 'legacy-library.csv'
        worker.TARGET = self.root / 'legacy-target.yaml'
        worker.PDB = self.root / 'legacy-receptor.pdb'
        worker.REFERENCE = self.root / 'legacy-reference.sdf'
        worker.RELEASE = None
        worker.RESEARCH_INPUT_ROOT = self.root
        self.config = ('target_id: EGFR\nresidue_number: 797\nwarhead_class: acrylamide\n'
                       'reaction_type: Michael Addition\nstructure:\n  carsidock_covres: A:CYS:773\n')
        self.pdb = 'ATOM      1  SG  CYS A 773       0.000   0.000   0.000\n'
        for path in (worker.SOURCE, worker.REFERENCE, worker.PROJECT / 'environment/activate_carsidock.sh',
                     worker.CARSI / 'checkpoints/carsidock_230731.ckpt', worker.CARSI / 'run_carsidockcov_screening.py',
                     self.root / 'rtms.ckpt'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SYNTHETIC infrastructure fixture; not scientific evidence\n')
        (worker.PROJECT / 'configs').mkdir()
        (worker.PROJECT / 'configs/local.inspire.yaml').write_text(
            'carsidock:\n  rtmscore_checkpoint: ' + str(self.root / 'rtms.ckpt') + '\n')
        worker.TARGET.write_text(self.config)
        worker.PDB.write_text(self.pdb)
        self.legacy = {str(path): path.read_bytes() for path in (worker.SOURCE, worker.TARGET, worker.PDB, worker.REFERENCE)}
        inputs = self.root / 'prepared-inputs-v1'
        inputs.mkdir()
        self.library = inputs / 'library-prefix.csv'
        # Include a quoted multiline Unicode field and duplicate rows. This is
        # CSV record order, not physical line counting or identity deduplication.
        self.rows = [[str(index), 'SYNTHETIC,记录\n' + str(index)] for index in range(20000)]
        self.rows[10] = self.rows[9][:]
        self.header = ['', 'synthetic_value']
        self.write_csv()
        (inputs / 'receptor.pdb').write_text('REMARK SYNTHETIC independent receptor\n' + self.pdb)
        (inputs / 'target.yaml').write_text('# SYNTHETIC independent config\n' + self.config)
        (inputs / 'reference.sdf').write_text('SYNTHETIC reference; not a real molecule\n')
        self.manifest = {'schema': 'drug-design-frozen-library-prefix-v1',
                         'operation': 'data-freeze-only-no-molecule-screening',
                         'source': {'path': str(worker.SOURCE), 'sha256': worker.sha(worker.SOURCE)},
                         'subset': {'sha256': worker.sha(self.library), 'size_bytes': self.library.stat().st_size,
                                    'encoding': 'utf-8', 'line_ending': 'LF'},
                         'selection': {'records': 20000, 'method': 'file_order_prefix', 'source_record_index_base': 0,
                                       'duplicates_preserved': True, 'unique_row_hashes': 19999},
                         'header': self.header,
                         'records': [{'source_record_index': index, 'row_sha256': worker.digest(row)}
                                     for index, row in enumerate(self.rows)]}
        self.manifestpath = inputs / 'prefix.manifest.json'
        worker.write(self.manifestpath, self.manifest)
        def binding(path):
            return {'path': str(path), 'sha256': worker.sha(path)}
        self.inputs = {'version': 'screening-research-inputs-v1',
                       'library': {**binding(self.library), 'records': 20000, 'selection': 'frozen-file-order-prefix',
                                   'source': binding(worker.SOURCE), 'manifest': binding(self.manifestpath)},
                       'receptor': binding(inputs / 'receptor.pdb'),
                       'science_config': binding(inputs / 'target.yaml'),
                       'reference': binding(inputs / 'reference.sdf')}
        snapshot = worker.snapshot(self.inputs)
        self.plan = {'version': 'screening-executor-v1', 'id': 'screen-' + 'a' * 24, 'target': 'EGFR', 'site': 'C797',
                     'library': 'CovInDB/Virtual_Screening_Library.csv', 'scope': 'bounded',
                     'max_records': 20000, 'shortlist': 5, 'max_points': 1, 'seed': 42,
                     'gates': {'min_carsidock_success': 0.75, 'min_rtmscore_coverage': 0.95},
                     'runtime': snapshot['runtime'], 'assets': snapshot['assets'], 'research_inputs': self.inputs,
                     'automation': {'version': 'screening-resource-planner-v1', 'selected_mode': 'research_pilot_v1',
                                    'requested_mode': 'research_pilot_v1', 'adjusted': False},
                     'steps': [{'phase': phase, 'minutes': minutes, 'max_candidates': 5,
                                'resource': {'quota': '1,10,200' if phase in ('carsidock', 'rtmscore') else '0,4,16',
                                             'points_per_hour': 1}}
                               for phase, minutes in zip(worker.PHASES, [15, 10, 5, 5])]}

    def tearDown(self):
        for name, value in self.original.items():
            setattr(worker, name, value)
        self.temp.cleanup()

    def write_csv(self):
        with self.library.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream, lineterminator='\n')
            writer.writerow(self.header)
            writer.writerows(self.rows)

    def refreeze_manifest(self):
        worker.write(self.manifestpath, self.manifest)
        self.inputs['library']['manifest']['sha256'] = worker.sha(self.manifestpath)
        self.plan['assets'][str(self.manifestpath)] = worker.sha(self.manifestpath)

    def test_snapshot_commands_and_source_receipt_use_same_explicit_roles(self):
        worker.validate(self.plan, worker.digest(self.plan))
        worker.verify_assets(self.plan)
        snapshot = worker.snapshot(self.inputs)
        self.assertEqual(snapshot['source'], str(self.library))
        self.assertEqual(snapshot['original_library_source'], self.inputs['library']['source'])
        self.assertNotIn(str(worker.SOURCE), snapshot['assets'])
        self.assertNotIn(str(worker.PDB), snapshot['assets'])
        for phase in worker.PHASES[:-1]:
            args = worker.command(self.plan, phase, worker.directory(self.plan['id']) / phase)
            if phase == 'prepare':
                self.assertEqual(args[args.index('--input') + 1], self.inputs['library']['path'])
                self.assertEqual(args[args.index('--target-config') + 1], self.inputs['science_config']['path'])
                self.assertEqual(args[args.index('--max-rows') + 1], '20000')
            else:
                self.assertEqual(args[args.index('--pdb') + 1], self.inputs['receptor']['path'])
                self.assertEqual(args[args.index('--reference-ligand') + 1], self.inputs['reference']['path'])
        self.assertEqual(self.legacy, {name: Path(name).read_bytes() for name in self.legacy})

    def test_every_actual_role_is_rehashed_before_snapshot_and_stage_command(self):
        files = [self.inputs[key] for key in ('library', 'receptor', 'science_config', 'reference')]
        files.append(self.inputs['library']['manifest'])
        for item in files:
            path = Path(item['path'])
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'checksum'):
                worker.snapshot(self.inputs)
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'checksum'):
                worker.command(self.plan, 'prepare', self.root / 'prepare')
            path.write_bytes(original)

    def test_source_provenance_is_not_used_as_actual_input(self):
        # The original full library is provenance only: a later change is not a
        # reason to silently read it, rehash it or replace the frozen prefix.
        worker.SOURCE.unlink()
        worker.verify_assets(self.plan)
        self.assertEqual(worker.snapshot(self.inputs)['source'], str(self.library))

    def test_actual_stage_fails_before_child_when_prepared_input_changed(self):
        base = worker.directory(self.plan['id'])
        base.mkdir(parents=True)
        worker.write(base / 'plan.json', self.plan)
        Path(self.inputs['receptor']['path']).write_text('changed before execution')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            worker.execute(self.plan['id'], 'prepare', worker.digest(self.plan))
        self.assertFalse((base / 'prepare/engine.log').exists())
        self.assertFalse((base / 'prepare/manifest.json').exists())
        self.assertEqual(json.loads((base / 'prepare/error.json').read_text())['status'], 'FAILED')

    def test_refrozen_manifest_cannot_relabel_header_order_or_original_source(self):
        original = copy.deepcopy(self.manifest)
        for mutation in ('header', 'index', 'row', 'source', 'count', 'unique', 'selection'):
            self.manifest = copy.deepcopy(original)
            if mutation == 'header':
                self.manifest['header'] = ['id', 'wrong']
            elif mutation == 'index':
                self.manifest['records'][1]['source_record_index'] = 0
            elif mutation == 'row':
                self.manifest['records'][0], self.manifest['records'][1] = self.manifest['records'][1], self.manifest['records'][0]
            elif mutation == 'source':
                self.manifest['source']['sha256'] = self.inputs['library']['sha256']
            elif mutation == 'count':
                self.manifest['records'].pop()
            elif mutation == 'unique':
                self.manifest['selection']['unique_row_hashes'] = 20000
            else:
                self.manifest['selection']['method'] = 'random_sample'
            self.refreeze_manifest()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                worker.verify_research_inputs(self.inputs, self.plan['assets'])

    def test_rebound_csv_still_requires_exact_header_records_order_and_width(self):
        original = copy.deepcopy(self.rows)
        for mutation in ('extra', 'missing', 'reordered', 'width'):
            self.rows = copy.deepcopy(original)
            if mutation == 'extra':
                self.rows.append(['20000', 'extra'])
            elif mutation == 'missing':
                self.rows.pop()
            elif mutation == 'reordered':
                self.rows[0], self.rows[1] = self.rows[1], self.rows[0]
            else:
                self.rows[0].append('extra column')
            self.write_csv()
            self.inputs['library']['sha256'] = worker.sha(self.library)
            self.plan['assets'][str(self.library)] = worker.sha(self.library)
            self.manifest['subset'].update(sha256=worker.sha(self.library), size_bytes=self.library.stat().st_size)
            self.refreeze_manifest()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                worker.verify_research_inputs(self.inputs, self.plan['assets'])

    def test_worker_rejects_asset_mode_version_and_path_role_substitutions(self):
        for mutation in ('asset', 'mode', 'version', 'alias', 'escape', 'unknown'):
            plan = copy.deepcopy(self.plan)
            if mutation == 'asset':
                del plan['assets'][self.inputs['reference']['path']]
            elif mutation == 'mode':
                plan['automation']['selected_mode'] = 'standard'
            elif mutation == 'version':
                plan['research_inputs']['version'] = 'unsupported-v2'
            elif mutation == 'alias':
                plan['research_inputs']['reference'] = plan['research_inputs']['receptor']
            elif mutation == 'escape':
                plan['research_inputs']['reference']['path'] = str(self.root) + '/../outside.sdf'
            else:
                plan['research_inputs']['command'] = 'anything'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                worker.validate(plan, worker.digest(plan))

    def test_role_files_cannot_be_redirected_even_to_identical_bytes(self):
        path = Path(self.inputs['reference']['path'])
        target = self.root / 'other-reference.sdf'
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'redirected'):
            worker.verify_research_inputs(self.inputs, self.plan['assets'])

    def test_prepared_receipt_cannot_claim_original_library_or_different_config(self):
        receipt = {'source': {'path': str(self.library), 'sha256': self.inputs['library']['sha256'],
                              'max_rows': 20000, 'records_read': 20000, 'record_selection': 'file_order_prefix'},
                   'target': {'config_path': self.inputs['science_config']['path'],
                              'config_sha256': self.inputs['science_config']['sha256']}}
        worker.verify_prepared_source(self.plan, receipt)
        for role, field, value in [('source', 'path', str(worker.SOURCE)),
                                   ('source', 'sha256', self.inputs['library']['source']['sha256']),
                                   ('source', 'records_read', 19999), ('source', 'record_selection', 'complete_file'),
                                   ('target', 'config_path', str(worker.TARGET)),
                                   ('target', 'config_sha256', worker.sha(worker.TARGET))]:
            invalid = copy.deepcopy(receipt)
            invalid[role][field] = value
            with self.subTest(role=role, field=field), self.assertRaises(ValueError):
                worker.verify_prepared_source(self.plan, invalid)

    def test_absent_extension_keeps_legacy_snapshot_and_commands(self):
        snapshot = worker.snapshot()
        self.assertNotIn('research_inputs', snapshot)
        self.assertNotIn('original_library_source', snapshot)
        self.assertEqual(snapshot['source'], str(worker.SOURCE))
        self.assertIn(str(worker.PDB), snapshot['assets'])
        plan = copy.deepcopy(self.plan)
        del plan['research_inputs']
        plan['assets'] = snapshot['assets']
        worker.verify_assets(plan)
        args = worker.command(plan, 'prepare', self.root / 'prepare')
        self.assertEqual(args[args.index('--input') + 1], str(worker.SOURCE))
        args = worker.command(plan, 'carsidock', self.root / 'carsidock')
        self.assertEqual(args[args.index('--pdb') + 1], str(worker.PDB))

    def zero_files(self):
        import pandas as pd
        dest = worker.directory(self.plan['id']) / 'prepare'
        dest.mkdir(parents=True)
        worker.write(dest.parent / 'plan.json', self.plan)
        (dest / 'process.lock').touch()
        worker.write(dest / 'started.json', {'pid': 1, 'at': 10, 'plan_digest': worker.digest(self.plan)})
        worker.write(dest / 'error.json', {'task_id': self.plan['id'], 'phase': 'prepare',
                     'plan_digest': worker.digest(self.plan), 'status': 'FAILED', 'runtime_seconds': 0.2,
                     'failure_kind': 'zero_candidates', 'exit_code': 2,
                     'error': 'SYNTHETIC exit diagnostic; text is not a classification input'})
        source = {'schema_version': '1.0', 'diagnostics_version': 'prescreen-rejections-v1', 'status': 'completed',
                  'selection_outcome': 'NO_CANDIDATES_SELECTED',
                  'source': {'path': str(self.library), 'sha256': self.inputs['library']['sha256'],
                             'max_rows': 20000, 'records_read': 20000, 'record_selection': 'file_order_prefix'},
                  'target': {'config_path': self.inputs['science_config']['path'],
                             'config_sha256': self.inputs['science_config']['sha256']},
                  'selection': {'top_n_requested': 5, 'top_n_selected': 0},
                  'counts': {'input_rows': 20000, 'parsed': 20000, 'unique': 19999, 'duplicates': 1,
                             'invalid': 0, 'hard_gate_pass': 0, 'selected': 0},
                  'quality': {'rdkit_parse_rate': 1.0, 'selected_warhead_mapping_rate': 0.0,
                              'identity_loss_count': 0, 'failure_table_present': True},
                  'quality_gates': {'rdkit_parse_rate_ge_0_99': True, 'selected_warhead_mapping_rate_ge_0_98': False,
                                    'identity_loss_zero': True, 'failure_table_present': True}}
        worker.write(dest / 'stage2_selection_manifest.json', source)
        pd.DataFrame().to_parquet(dest / 'stage2_candidates.parquet', index=False)
        pd.DataFrame({'eligible': [False] * 19999}).to_parquet(dest / 'covindb_master.parquet', index=False)
        pd.DataFrame({'failure_reason': ['SYNTHETIC'] * 19999}).to_parquet(dest / 'covindb_rejected.parquet', index=False)
        return dest, source

    def test_zero_selection_is_recomputed_and_delivered_only_as_failed_stage_diagnostic(self):
        dest, source = self.zero_files()
        zero = worker.zero_candidates(self.plan, dest)
        self.assertEqual(zero['candidate_count'], 0)
        self.assertEqual(zero['input_rows'], 20000)
        self.assertEqual(zero['actual_source'], source['source'])
        self.assertFalse(zero['scientific_quality_verified'])
        worker.write(dest / 'zero-candidates.json', zero)
        result = worker.read_failure(self.plan['id'], 'prepare', worker.digest(self.plan))
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(set(result['artifacts']), {'error.json', 'started.json', 'zero-candidates.json', 'stage2_selection_manifest.json'})
        self.assertEqual(result['artifacts']['zero-candidates.json']['sha256'], worker.sha(dest / 'zero-candidates.json'))
        self.assertFalse((dest / 'manifest.json').exists())
        self.assertEqual(worker.read_failure(self.plan['id'], 'prepare', worker.digest(self.plan), worker.digest(result)), result)
        source['source']['sha256'] = '0' * 64
        worker.write(dest / 'stage2_selection_manifest.json', source)
        with self.assertRaises(ValueError):
            worker.read_failure(self.plan['id'], 'prepare', worker.digest(self.plan), worker.digest(result))

    def test_zero_selection_refuses_incomplete_failed_quality_nonzero_or_changed_tables(self):
        import pandas as pd
        dest, original = self.zero_files()
        for mutation in ('not_completed', 'different_source', 'different_config', 'parse_failed', 'identity_lost', 'candidate', 'eligible'):
            source = copy.deepcopy(original)
            pd.DataFrame().to_parquet(dest / 'stage2_candidates.parquet', index=False)
            pd.DataFrame({'eligible': [False] * 19999}).to_parquet(dest / 'covindb_master.parquet', index=False)
            if mutation == 'not_completed':
                source['status'] = 'running'
            elif mutation == 'different_source':
                source['source']['path'] = str(worker.SOURCE)
            elif mutation == 'different_config':
                source['target']['config_sha256'] = worker.sha(worker.TARGET)
            elif mutation == 'parse_failed':
                source['counts']['parsed'] = 1
            elif mutation == 'identity_lost':
                source['counts']['unique'] = 19998
            elif mutation == 'candidate':
                pd.DataFrame({'compound_id': ['SYNTHETIC']}).to_parquet(dest / 'stage2_candidates.parquet', index=False)
            else:
                pd.DataFrame({'eligible': [True] + [False] * 19998}).to_parquet(dest / 'covindb_master.parquet', index=False)
            worker.write(dest / 'stage2_selection_manifest.json', source)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                worker.zero_candidates(self.plan, dest)

    def test_zero_diagnostic_cannot_be_forged_or_redirected(self):
        dest, _ = self.zero_files()
        zero = worker.zero_candidates(self.plan, dest)
        worker.write(dest / 'zero-candidates.json', {**zero, 'input_rows': 999})
        with self.assertRaisesRegex(ValueError, 'recomputed'):
            worker.read_failure(self.plan['id'], 'prepare', worker.digest(self.plan))
        path = dest / 'zero-candidates.json'
        path.unlink()
        path.symlink_to(dest / 'stage2_selection_manifest.json')
        with self.assertRaisesRegex(ValueError, 'regular file'):
            worker.read_failure(self.plan['id'], 'prepare', worker.digest(self.plan))

    def test_actual_failed_child_retains_valid_zero_selection_without_success_manifest(self):
        import time
        dest, _ = self.zero_files()
        for name in ('started.json', 'error.json'):
            (dest / name).unlink()
        entry = worker.PROJECT.parent / 'routes/library-first/scripts/run_covindb_prescreen.py'
        entry.parent.mkdir(parents=True)
        entry.write_text('# SYNTHETIC exit-only child; all fixture outputs are already present.\nraise SystemExit(2)\n')
        self.plan['finish_before'] = int(time.time() * 1000) + 60000
        worker.write(dest.parent / 'plan.json', self.plan)
        with self.assertRaisesRegex(ValueError, 'engine exited with code 2'):
            worker.execute(self.plan['id'], 'prepare', worker.digest(self.plan))
        zero = json.loads((dest / 'zero-candidates.json').read_text())
        self.assertEqual(zero['outcome'], 'NO_CANDIDATES_SELECTED')
        self.assertEqual(zero['plan_digest'], worker.digest(self.plan))
        self.assertFalse((dest / 'manifest.json').exists())
        self.assertEqual(worker.read_failure(self.plan['id'], 'prepare', worker.digest(self.plan))['status'], 'FAILED')

    def test_unrelated_exit_and_budget_failures_cannot_be_hidden_by_zero_output_files(self):
        import time
        dest, _ = self.zero_files()
        entry = worker.PROJECT.parent / 'routes/library-first/scripts/run_covindb_prescreen.py'
        entry.parent.mkdir(parents=True)
        entry.write_text('# SYNTHETIC unrelated child failure\nraise SystemExit(3)\n')
        for expired in (False, True):
            for name in ('started.json', 'error.json', 'environment.json', 'engine.log', 'research-inputs.json'):
                (dest / name).unlink(missing_ok=True)
            self.plan['finish_before'] = int(time.time() * 1000) + (-1000 if expired else 60000)
            worker.write(dest.parent / 'plan.json', self.plan)
            with self.assertRaises(ValueError):
                worker.execute(self.plan['id'], 'prepare', worker.digest(self.plan))
            self.assertFalse((dest / 'zero-candidates.json').exists())
            error = json.loads((dest / 'error.json').read_text())
            self.assertNotIn('failure_kind', error)
            result = worker.read_failure(self.plan['id'], 'prepare', worker.digest(self.plan))
            self.assertNotIn('stage2_selection_manifest.json', result['artifacts'])


if __name__ == '__main__':
    unittest.main()
