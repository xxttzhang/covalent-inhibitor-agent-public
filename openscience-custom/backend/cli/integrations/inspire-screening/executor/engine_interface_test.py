"""Actual AST/filesystem tests, without engine execution or mocked dependencies."""
import tempfile
import unittest
from pathlib import Path

from engine_interface import inspect
import code_release


class EngineInterfaceTest(unittest.TestCase):
    def test_geometry_support_is_reported_only_by_a_release_with_the_actual_reader_and_helper(self):
        with tempfile.TemporaryDirectory(prefix='geometry-interface-test-') as root:
            root = Path(root)
            project = Path(__file__).resolve().parents[6] / 'science'
            archive = code_release.build(project, root)
            released = code_release.install(archive['archive'], root, archive['release_id'], archive['manifest_sha256'])
            science = Path(released['root']) / 'science'
            self.assertEqual(inspect(science, 'af3')['geometry_contracts'], ['af3-geometry-evidence-v1'])
            self.assertEqual(inspect(science, 'covdock')['geometry_contracts'], [])
            self.assertTrue(inspect(science, 'covdock')['original_structure_readback_supported'])
            policies = ['refinement-structural-qc-v1', 'refinement-structural-qc-v2-provisional']
            self.assertEqual(inspect(science, 'af3')['quality_policies'], policies)
            self.assertEqual(inspect(science, 'covdock')['quality_policies'], policies)
            entry = science / 'scripts/run_covdock_lo_registered.py'
            entry.chmod(0o644)
            original = entry.read_text()
            entry.write_text(original.replace('--gate-algorithm-version', '--unwired-policy'))
            self.assertEqual(inspect(science, 'covdock')['quality_policies'], policies[:1])
            entry.write_text(original)
            reader = science.parent / 'refinement_readback.py'
            reader.chmod(0o644)
            reader.write_text('# missing reader in isolated test fixture')
            self.assertEqual(inspect(science, 'af3')['geometry_contracts'], [])
            self.assertFalse(inspect(science, 'covdock')['original_structure_readback_supported'])
            self.assertEqual(inspect(science, 'af3')['quality_policies'], [])

    def test_current_local_registered_entries_have_exact_input_interface(self):
        project = Path(__file__).resolve().parents[6] / 'science'
        for engine in ('af3', 'covdock'):
            result = inspect(project, engine)
            self.assertTrue(result['passes_static_checks'])
            self.assertEqual(result['missing_options'], [])
            self.assertTrue(result['exact_input_guard_present'])
            self.assertFalse(result['runtime_verified'])
            self.assertFalse(result['resource_approval_granted'])
            self.assertEqual(inspect(project, engine, result['entry_sha256']), result)
            if engine == 'covdock':
                self.assertIn('core', result['input_score_sources'])
                self.assertIn('--input-score-source', result['required_options'])

    def test_covdock_exact_input_flags_without_score_provenance_are_not_enough(self):
        with tempfile.TemporaryDirectory(prefix='engine-interface-test-') as root:
            project = Path(root)
            entry = project / 'scripts/run_covdock_lo_registered.py'
            entry.parent.mkdir()
            helper = project / 'src/covscreen/refinement_input.py'
            helper.parent.mkdir(parents=True)
            helper.write_text('# fixture only')
            entry.write_text('def main():\n' + ''.join('    parser.add_argument(' + repr(option) + ')\n'
                for option in ('--candidate-table', '--expected-input-count', '--expected-input-sha256', '--expected-identity-sha256')) +
                '    contract = verify_refinement_input(args.candidate_table, args.expected_input_count, args.expected_input_sha256, args.expected_identity_sha256)\n')
            result = inspect(project, 'covdock')
            self.assertTrue(result['exact_input_guard_present'])
            self.assertFalse(result['passes_static_checks'])
            self.assertEqual(result['missing_options'], ['--input-score-source'])

    def test_old_entry_and_flag_only_implementation_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix='engine-interface-test-') as root:
            project = Path(root)
            entry = project / 'scripts/run_af3_registered_batch.py'
            entry.parent.mkdir()
            entry.write_text('def main():\n    parser.add_argument("--candidate-table")\n')
            result = inspect(project, 'af3')
            self.assertFalse(result['passes_static_checks'])
            self.assertEqual(len(result['missing_options']), 3)
            helper = project / 'src/covscreen/refinement_input.py'
            helper.parent.mkdir(parents=True)
            helper.write_text('# fixture only')
            entry.write_text('def main():\n' + ''.join('    parser.add_argument(' + repr(option) + ')\n'
                             for option in result['required_options']))
            result = inspect(project, 'af3')
            self.assertEqual(result['missing_options'], [])
            self.assertFalse(result['passes_static_checks'])
            self.assertFalse(result['exact_input_guard_present'])
            with self.assertRaisesRegex(ValueError, 'source changed'):
                inspect(project, 'af3', '0' * 64)

    def test_entry_cannot_escape_project(self):
        with tempfile.TemporaryDirectory(prefix='engine-interface-test-') as root:
            project = Path(root) / 'project'
            (project / 'scripts').mkdir(parents=True)
            outside = Path(root) / 'outside.py'
            outside.write_text('pass')
            (project / 'scripts/run_af3_registered_batch.py').symlink_to(outside)
            with self.assertRaises(ValueError):
                inspect(project, 'af3')


if __name__ == '__main__':
    unittest.main()
