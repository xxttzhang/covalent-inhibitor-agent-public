"""Local subprocess/file diagnostics only; no real licenses or molecular engines."""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = types.ModuleType('test_embedded_refinement_diagnostic')
exec(compile((HERE / 'runtime_probe.py').read_text() + '\n' + (HERE / 'refinement_diagnostic.py').read_text(),
             '<embedded-diagnostic>', 'exec'), MODULE.__dict__)


class RefinementDiagnosticTest(unittest.TestCase):
    def test_environment_excludes_injection_without_mutating_parent(self):
        keys = ('LD_PRELOAD', 'LD_AUDIT', 'PYTHONPATH', 'PYTHONHOME')
        previous = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ[key] = 'UNTRUSTED_TEST_VALUE'
            clean = MODULE.environment()
            for key in keys:
                self.assertNotIn(key, clean)
                self.assertEqual(os.environ[key], 'UNTRUSTED_TEST_VALUE')
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_configuration_uses_real_registered_guard_without_reading_license_contents(self):
        science = Path(__file__).resolve().parents[6] / 'science'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            license_file = root / 'official.lic'
            license_file.write_text('PRIVATE_LICENSE_TEST_NOT_AN_ENTITLEMENT')
            self.assertIsNone(MODULE.license_configuration(science, license_file))
            marker = root / 'patcher'
            marker.write_text('NOT_EXECUTABLE_TEST_MARKER')
            result = MODULE.license_configuration(science, license_file)
            self.assertIsNone(result)
            self.assertEqual(MODULE.license_configuration(science, root / 'patcher' / 'license'),
                             'LICENSE_CONFIGURATION_REVIEW_REQUIRED')
            self.assertEqual(marker.read_text(), 'NOT_EXECUTABLE_TEST_MARKER')
            self.assertEqual(license_file.read_text(), 'PRIVATE_LICENSE_TEST_NOT_AN_ENTITLEMENT')
            self.assertEqual(MODULE.license_configuration(root / 'missing', license_file),
                             'LICENSE_CONFIGURATION_UNAVAILABLE')

    def test_vendor_entitlement_protocol_requires_both_licenses_and_owned_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'vendor_fixture.py'
            source.write_text('# non-scientific protocol fixture\n')
            tool = root / 'run'
            for covdock, ligprep, sha in [(True, True, MODULE.hashlib.sha256(source.read_bytes()).hexdigest()),
                (True, False, MODULE.hashlib.sha256(source.read_bytes()).hexdigest()), (True, True, '0' * 64)]:
                response = {'covdock_available': covdock, 'ligprep_available': ligprep, 'source_path': str(source), 'source_sha256': sha}
                tool.write_text('#!' + sys.executable + '\nprint("PRIVATE_LICENSE_WARNING")\nprint(' +
                    repr('COVDOCK_ENTITLEMENTS=' + json.dumps(response)) + ')\n')
                tool.chmod(0o755)
                result = MODULE.entitlement_probe({'schrodinger_root': str(root)}, MODULE.environment())
                self.assertEqual(result['state'] == 'PASSED', covdock and ligprep and sha != '0' * 64)
                self.assertNotIn('PRIVATE_LICENSE_WARNING', json.dumps(result))
            compile(MODULE.ENTITLEMENT_CODE, '<vendor-diagnostic>', 'exec')
            self.assertNotIn('.launch(', MODULE.ENTITLEMENT_CODE)

    def test_parameter_reader_contract_is_cpu_only_and_sanitized(self):
        # This executable tests the subprocess protocol, not scientific weights.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'src/alphafold3/model/params.py'
            source.parent.mkdir(parents=True)
            source.write_text('# protocol fixture, not AF3\n')
            tool = root / 'python'
            args = root / 'args.json'
            runtime = {'runner': str(root / 'run_alphafold.py'), 'af3_python': str(tool), 'model_dir': str(root / 'models')}
            good = {'state': 'PASSED', 'code': 'CPU_PARAMETERS_LOADED', 'tensor_count': 2, 'parameter_bytes': 12,
                    'schema_sha256': 'a' * 64, 'error_type': None, 'phase': 'completed',
                    'loader_path': str(source), 'loader_sha256': MODULE.hashlib.sha256(source.read_bytes()).hexdigest()}
            for payload, passed in [(good, True), ({**good, 'tensor_count': 0}, False),
                ({**good, 'error_type': 'PRIVATE_SECRET'}, False), ({**good, 'raw': 'PRIVATE_SECRET'}, False),
                ({**good, 'state': 'UNVERIFIED', 'code': 'CPU_PARAMETERS_LOAD_FAILED', 'tensor_count': None,
                  'parameter_bytes': None, 'schema_sha256': None, 'error_type': 'ValueError', 'phase': 'parameter_loading'}, False)]:
                tool.write_text('#!' + sys.executable + '\nimport sys,json,os\nfrom pathlib import Path\n'
                    'Path(' + repr(str(args)) + ').write_text(json.dumps({"args":sys.argv[1:],"platform":os.environ["JAX_PLATFORMS"],"gpu":os.environ["CUDA_VISIBLE_DEVICES"]}))\n'
                    'print("warning PRIVATE_SECRET")\nprint(' + repr('AF3_CPU_PARAMETERS=' + json.dumps(payload)) + ')\n')
                tool.chmod(0o755)
                result = MODULE.weights_probe(runtime)
                self.assertEqual(result['state'] == 'PASSED', passed)
                self.assertNotIn('PRIVATE_SECRET', json.dumps(result))
                if result['code'] == 'CPU_PARAMETERS_INVALID_OUTPUT':
                    self.assertIsNone(result['loader_sha256'])
                else:
                    self.assertEqual(result['loader_sha256'], MODULE.hashlib.sha256(source.read_bytes()).hexdigest())
                command = json.loads(args.read_text())
                self.assertEqual(command['platform'], 'cpu')
                self.assertEqual(command['gpu'], '')
                self.assertEqual(command['args'][:3], ['-I', '-B', '-c'])
                self.assertIn('params.get_model_haiku_params(directory)', command['args'][3])
                self.assertNotIn('run_inference', command['args'][3])

    def test_missing_parameter_interpreter_cannot_claim_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            result = MODULE.weights_probe({'runner': directory + '/missing.py', 'af3_python': '/not-executable', 'model_dir': directory})
            self.assertEqual(result['state'], 'UNVERIFIED')
            self.assertIsNone(result['returncode'])
            self.assertIsNone(result['tensor_count'])

    def test_import_timeout_is_not_reported_as_missing_dependency_and_errors_are_sanitized(self):
        result = MODULE.import_status({'passed': False, 'error_type': 'TimeoutExpired', 'seconds_limit': 20})
        self.assertEqual(result['reason'], 'TIMEOUT')
        self.assertEqual(result['seconds_limit'], 20)
        self.assertEqual(result['failed_modules'], [])
        result = MODULE.import_status({'passed': False, 'imports': {'json': {'imported': True},
            'missing': {'imported': False, 'error_type': 'ModuleNotFoundError', 'message': 'SECRET_CONTENT'}}})
        self.assertEqual(result['reason'], 'IMPORT_ERROR')
        self.assertEqual(result['failed_modules'], [{'module': 'missing', 'error_type': 'ModuleNotFoundError'}])
        self.assertNotIn('SECRET_CONTENT', json.dumps(result))

    def test_streamed_asset_hash_never_returns_file_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'fixture.lic'
            file.write_text('PRIVATE_TEST_CONTENT_DO_NOT_PUBLISH')
            result = MODULE.fingerprint(file)
            self.assertEqual(result['state'], 'HASHED')
            self.assertNotIn('PRIVATE_TEST', json.dumps(result))
            self.assertEqual(result['sha256'], MODULE.hashlib.sha256(file.read_bytes()).hexdigest())
            self.assertEqual(MODULE.fingerprint(file, limit=1)['state'], 'INVALID_FILE')
            self.assertEqual(MODULE.fingerprint(Path(directory) / 'missing')['state'], 'UNAVAILABLE')

    def test_license_errors_are_typed_and_zero_exit_does_not_prove_entitlement(self):
        for returncode, output, expected in [
            (17, 'Invalid license file syntax PRIVATE_TEST_SECRET', 'INVALID_LICENSE_SYNTAX'),
            (0, 'Error: No features found in license file. PRIVATE_TEST_SECRET', 'NO_LICENSE_FEATURES'),
            (2, 'Unable to checkout PRIVATE_TEST_SECRET', 'LICENSE_CHECKOUT_FAILED'),
            (0, 'Checked out MMLIBS successfully', 'COVDOCK_ENTITLEMENT_NOT_TESTED'),
        ]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'utilities').mkdir()
                tool = root / 'utilities/lictest'
                args = root / 'args.json'
                tool.write_text('#!' + sys.executable + '\nimport sys,json\nfrom pathlib import Path\n'
                    'Path(' + repr(str(args)) + ').write_text(json.dumps(sys.argv[1:]))\n'
                    'print(' + repr(output) + ')\nraise SystemExit(' + str(returncode) + ')\n')
                tool.chmod(0o755)
                result = MODULE.license_probe(root, root / 'fixture.lic', MODULE.environment())
                self.assertEqual(result['code'], expected)
                self.assertFalse(result['covdock_entitlement_verified'])
                self.assertNotIn('PRIVATE_TEST_SECRET', json.dumps(result))
                self.assertEqual(json.loads(args.read_text()), ['-f', str(root / 'fixture.lic'), '-l', 'MMLIBS'])
                self.assertEqual(result['state'], 'UNVERIFIED' if returncode == 0 and not output.startswith('Error') else 'FAILED')

    def test_native_process_timeout_does_not_claim_checkout(self):
        result = MODULE.bounded_process([sys.executable, '-B', '-c', 'import time;time.sleep(10)'],
                                        MODULE.environment(), os.getcwd(), timeout=0.05)
        self.assertEqual(result['state'], 'TIMEOUT')
        self.assertEqual(result['output'], '')

    def test_af3_target_uses_sequence_cysteine_not_raw_pdb_number(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'protein.json'
            runtime = {'source_data_json': str(file), 'protein_chain': 'A', 'cysteine_sequence_index': 3}
            protein = {'id': 'A', 'sequence': 'AACAAA', 'unpairedMsa': 'PRIVATE_MSA', 'pairedMsa': '', 'templates': [{}]}
            file.write_text(json.dumps({'dialect': 'alphafold3', 'sequences': [{'protein': protein}]}))
            result = MODULE.protein_probe(runtime)
            self.assertEqual(result['state'], 'PASSED')
            self.assertEqual(result['sequence_length'], 6)
            self.assertNotIn('PRIVATE_MSA', json.dumps(result))
            self.assertEqual(MODULE.protein_probe({**runtime, 'cysteine_sequence_index': 4})['state'], 'UNVERIFIED')
            self.assertEqual(MODULE.protein_probe({**runtime, 'protein_chain': 'B'})['state'], 'UNVERIFIED')

    def test_receptor_probe_requires_unique_cysteine_sg_and_ca_and_single_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = root / 'run'
            args = root / 'args.json'
            runtime = {'schrodinger_root': str(root), 'receptor': '/test-fixture.mae', 'attachment_residue': 'A:797'}
            for single, sg, ca in [(True, 1, 1), (True, 0, 0), (True, 2, 1), (False, 1, 1)]:
                response = {'single_structure': single, 'cys_sg_count': sg, 'cys_ca_count': ca}
                tool.write_text('#!' + sys.executable + '\nimport sys,json\nfrom pathlib import Path\n'
                    'Path(' + repr(str(args)) + ').write_text(json.dumps(sys.argv[1:]))\n'
                    'print(' + repr(json.dumps(response)) + ')\n')
                tool.chmod(0o755)
                result = MODULE.receptor_probe(runtime, MODULE.environment())
                self.assertEqual(result['state'], 'PASSED' if single and sg == ca == 1 else 'UNVERIFIED')
                command = json.loads(args.read_text())
                self.assertEqual(command[:2], ['python3', '-c'])
                self.assertEqual(command[-2:], ['/test-fixture.mae', 'A:797'])
                self.assertNotIn('covalent_docking', command)


if __name__ == '__main__':
    unittest.main()
