"""Real source packaging/install/import tests, not scientific engine execution."""
import io
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import code_release
from engine_interface import inspect


class CodeReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='screening-code-release-test-')
        self.root = Path(self.temp.name)
        self.project = Path(__file__).resolve().parents[6] / 'science'
        self.config = self.root / 'runtime.yaml'
        self.config.write_text('project_root: /explicit/test/data\nruntime: {}\ncarsidock:\n  rtmscore_checkpoint: /explicit/external/rtmscore.pt\n')
        self.package = code_release.build(self.project, self.root, self.config)
        self.releases = self.root / 'releases'
        self.releases.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def install(self):
        return code_release.install(self.package['archive'], self.releases,
                                    self.package['release_id'], self.package['manifest_sha256'])

    def test_deterministic_archive_and_idempotent_install_without_source_changes(self):
        original = (self.project / 'scripts/run_af3_registered_batch.py').read_bytes()
        other = self.root / 'other'
        other.mkdir()
        second = code_release.build(self.project, other, self.config)
        self.assertEqual(self.package['release_id'], second['release_id'])
        self.assertEqual(self.package['archive_sha256'], second['archive_sha256'])
        installed = self.install()
        before = {str(file): file.stat().st_mtime_ns for file in Path(installed['root']).rglob('*') if file.is_file()}
        self.assertEqual(self.install(), installed)
        self.assertEqual(before, {str(file): file.stat().st_mtime_ns for file in Path(installed['root']).rglob('*') if file.is_file()})
        self.assertEqual(original, (self.project / 'scripts/run_af3_registered_batch.py').read_bytes())
        self.assertTrue(all(code_release.allowed(name) for name in installed['files']))
        self.assertFalse(any(name.endswith(('.csv', '.parquet', '.ckpt', '.lic', '.env')) for name in installed['files']))
        for engine in ('af3', 'covdock'):
            self.assertTrue(inspect(Path(installed['root']) / 'science', engine)['passes_static_checks'])

    def test_parent_index_is_packaged_with_exact_bytes_and_no_arbitrary_data(self):
        installed = self.install()
        for name in code_release.PARENT_INDEX:
            self.assertEqual((Path(installed['root']) / name).read_bytes(),
                             (self.project / name.removeprefix('science/')).read_bytes())
        self.assertFalse(code_release.allowed('science/src/covscreen/data/private.tsv'))
        self.assertFalse(code_release.allowed('science/src/covscreen/data/credentials.json'))

    def test_external_runtime_configuration_is_bound_to_release_identity(self):
        self.config.write_text('project_root: /different/test/data\nruntime: {}\n')
        changed = code_release.build(self.project, self.root, self.config)
        self.assertNotEqual(changed['release_id'], self.package['release_id'])
        linked = self.root / 'linked.yaml'
        linked.symlink_to(self.config)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            code_release.build(self.project, self.root, linked)

    def test_released_docking_policy_imports_without_source_checkout(self):
        installed = self.install()
        root = Path(installed['root'])
        policy = root / code_release.POLICY
        self.assertEqual(policy.read_bytes(), (self.project / 'src/covscreen/docking-engine-policy.json').read_bytes())
        self.assertIn(code_release.POLICY, installed['files'])
        self.assertFalse(code_release.allowed('science/src/covscreen/unregistered-secret.json'))
        probe = subprocess.run([sys.executable, '-B', '-c',
            'import json; from covscreen.docking_policy import POLICY; print(json.dumps(POLICY))'],
            env={**os.environ, 'PYTHONPATH': str(root / 'science/src'), 'PYTHONDONTWRITEBYTECODE': '1'},
            cwd=self.root, capture_output=True, text=True, check=True, timeout=30)
        self.assertEqual(json.loads(probe.stdout), json.loads(policy.read_text()))
        self.assertEqual(json.loads(probe.stdout)['disabled_engines'], ['carsidock-cov'])

    def test_denovo_independent_adapter_and_stop_recovery_are_in_new_release(self):
        installed = self.install()
        required = ('evaluation/source.py', 'evaluation/prepare.py', 'evaluation/control.py',
                    'runtime/review_transport.py', 'runtime/worker.py',
                    'src/covscreen_de_novo/independent_review.py', 'src/covscreen_de_novo/control_review.py',
                    'src/covscreen_de_novo/chemical_review.py',
                    'src/covscreen_de_novo/prereaction.py', 'src/covscreen_de_novo/prereaction_runner.py',
                    'src/covscreen_de_novo/prereaction_workflow.py', 'scripts/run_prereaction_registered.py',
                    'registry/egfr_c797_v1/assets/0WM.cif')
        for name in required:
            self.assertIn('routes/de-novo/' + name, installed['files'])
        for name in ('read_covdock_control.py', 'extract_covdock_pose_coordinates.py', 'inspect_covdock_output.py'):
            self.assertIn('science/scripts/' + name, installed['files'])
        root = Path(installed['root'])
        self.assertEqual((root / 'routes/de-novo/runtime/worker.py').read_bytes(),
                         (self.project.parent / 'routes/de-novo/runtime/worker.py').read_bytes())

    def test_unchanged_module_is_imported_from_release_not_old_editable_source(self):
        installed = self.install()
        root = Path(installed['root'])
        env = {**os.environ, 'PYTHONPATH': str(root / 'science/src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        probe = subprocess.run([sys.executable, '-B', '-c',
                                'import covscreen.refinement_input; print(covscreen.refinement_input.__file__)'],
                               env=env, cwd=self.root, text=True, capture_output=True, check=True, timeout=30)
        self.assertEqual(Path(probe.stdout.strip()), root / 'science/src/covscreen/refinement_input.py')
        self.assertEqual(self.install(), installed)

    def test_mcp_registry_is_packaged_and_runs_without_the_editable_checkout(self):
        installed = self.install()
        root = Path(installed['root'])
        data = self.root / 'campaign-data'
        data.mkdir()
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
             'params': {'name': 'covalent_capabilities', 'arguments': {}}},
        ]
        probe = subprocess.run([sys.executable, '-B', str(root / 'science/scripts/openscience_mcp_server.py')],
            input=''.join(json.dumps(item) + '\n' for item in messages),
            env={**os.environ, 'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1', 'COVSCREEN_PROJECT_ROOT': str(data)},
            cwd=self.root, text=True, capture_output=True, check=True, timeout=30)
        self.assertEqual(probe.stderr, '')
        responses = [json.loads(line)['result'] for line in probe.stdout.splitlines()]
        names = {tool['name'] for tool in responses[0]['tools']}
        self.assertEqual(len(names), 68)
        self.assertIn('covalent_de_novo_review', names)
        self.assertIn('covalent_library_first_next_md_action', names)
        self.assertIn('covalent_library_first_resolve_target', names)
        self.assertIn('covalent_library_first_discover_structures', names)
        self.assertIn('covalent_library_first_acquire_structure', names)
        self.assertIn('covalent_library_first_prepare_receptor', names)
        self.assertIn('covalent_prepare_execution', names)
        self.assertEqual(names, set(responses[1]['structuredContent']['tools']))
        self.assertEqual(responses[1]['structuredContent']['project_root'], str(data.resolve()))
        self.assertTrue((root / 'science/src/covscreen/mcp/library_first.py').is_file())
        self.assertEqual(list(data.iterdir()), [])
        self.assertEqual(self.install(), installed)

    def test_release_mcp_entry_verifies_release_and_separate_data_configuration(self):
        installed = self.install()
        root = Path(installed['root'])
        data = self.root / 'verified-mcp-data'
        (data / 'configs').mkdir(parents=True)
        source_config = root / 'science/configs/local.inspire.yaml'
        (data / 'configs/local.inspire.yaml').write_bytes(source_config.read_bytes())
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        probe = subprocess.run(
            [sys.executable, '-B', str(root / 'mcp_entry.py'), '--data-root', str(data)],
            input=json.dumps(request) + '\n',
            env={**os.environ, 'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1'},
            cwd=self.root, text=True, capture_output=True, check=True, timeout=30,
        )
        response = json.loads(probe.stdout)['result']
        self.assertEqual(len({item['name'] for item in response['tools']}), 68)
        self.assertEqual(probe.stderr, '')
        self.assertFalse((data / 'vendor').exists())
        (data / 'configs/local.inspire.yaml').write_text('changed: true\n')
        rejected = subprocess.run(
            [sys.executable, '-B', str(root / 'mcp_entry.py'), '--data-root', str(data)],
            input=json.dumps(request) + '\n',
            env={**os.environ, 'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1'},
            cwd=self.root, text=True, capture_output=True, timeout=30,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('runtime configuration differs', rejected.stderr)

    def test_data_only_campaign_uses_vendors_from_immutable_release(self):
        installed = self.install()
        root = Path(installed['root'])
        data = self.root / 'data-only-campaign'
        data.mkdir()
        program = '''
import json, os
from pathlib import Path
from covscreen.execution import JobManager
manager = JobManager(Path(os.environ['CAMPAIGN_DATA']))
print(json.dumps({name: str(manager._bundled_vendor(name)) for name in ('PDBFixer', 'InterMol')}))
'''
        probe = subprocess.run(
            [sys.executable, '-B', '-c', program],
            env={**os.environ, 'PYTHONPATH': str(root / 'science/src'),
                 'PYTHONDONTWRITEBYTECODE': '1', 'CAMPAIGN_DATA': str(data)},
            cwd=self.root, text=True, capture_output=True, check=True, timeout=30,
        )
        vendors = json.loads(probe.stdout)
        self.assertEqual(Path(vendors['PDBFixer']), root / 'science/vendor/PDBFixer')
        self.assertEqual(Path(vendors['InterMol']), root / 'science/vendor/InterMol')
        self.assertFalse((data / 'vendor').exists())
        self.assertEqual(self.install(), installed)

    def test_unlicensed_ligand_evidence_retrieval_is_not_distributed(self):
        installed = self.install()
        root = Path(installed['root'])
        vendor = root / 'science/vendor/drug-idea-generator'
        self.assertFalse(vendor.exists())
        self.assertEqual(self.install(), installed)

    def test_route_code_and_legacy_imports_resolve_to_same_installed_source(self):
        installed = self.install()
        root = Path(installed['root'])
        env = {**os.environ, 'PYTHONPATH': str(root / 'science/src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        probe = subprocess.run([sys.executable, '-B', '-c',
            'import covscreen.library_chain as old; import covscreen_library_first.library_chain as new; '
            'assert old is new; print(new.__file__)'], env=env, cwd=self.root,
            text=True, capture_output=True, check=True, timeout=30)
        self.assertEqual(Path(probe.stdout.strip()), root / 'routes/library-first/src/covscreen_library_first/library_chain.py')
        # Test an extracted release from an unrelated cwd, without a checkout in PYTHONPATH.
        for location in ('routes/library-first/scripts', 'science/scripts'):
            probe = subprocess.run([sys.executable, '-B', str(root / location / 'run_covindb_prescreen.py'), '--help'],
                env={**env, 'PYTHONPATH': ''}, cwd=self.root, text=True, capture_output=True, timeout=30)
            self.assertEqual(probe.returncode, 0, probe.stderr)
        native = root / 'routes/library-first/scripts/parameterize_covalent_adduct.py'
        probe = subprocess.run([sys.executable, '-B', str(native), '--help'],
            env={**env, 'PYTHONPATH': ''}, cwd=self.root, text=True, capture_output=True, timeout=30)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn('--qc-sha256', probe.stdout)
        self.assertTrue((root / 'routes/library-first/src/covscreen_library_first/md_parameterization.py').is_file())
        self.assertEqual(self.install(), installed)

    def test_denovo_release_uses_canonical_source_and_shared_adapter(self):
        root = Path(self.install()['root'])
        env = {**os.environ, 'PYTHONPATH': str(root / 'science/src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        program = "import covscreen.de_novo as old; import covscreen_de_novo.de_novo as new; assert old is new; print(new.__file__)"
        probe = subprocess.run([sys.executable, '-B', '-c', program], env=env, cwd=self.root,
                               text=True, capture_output=True, check=True, timeout=30)
        self.assertEqual(Path(probe.stdout.strip()), root / 'routes/de-novo/src/covscreen_de_novo/de_novo.py')
        for location in ('routes/de-novo/scripts', 'science/scripts'):
            probe = subprocess.run([sys.executable, '-B', str(root / location / 'run_reinvent4_registered.py'), '--help'],
                env={**env, 'PYTHONPATH': ''}, cwd=self.root, text=True, capture_output=True, timeout=30)
            self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertTrue((root / 'science/scripts/run_difflinker_registered.py').is_file())
        self.assertFalse((root / 'routes/de-novo/scripts/run_difflinker_registered.py').exists())
        self.assertTrue((root / 'routes/de-novo/registry/egfr_c797_v1/assets/acrylamide_prereaction.sdf').is_file())

    def test_ligand_release_loads_both_routes_and_verifies_frozen_source(self):
        installed = self.install()
        root = Path(installed['root'])
        env = {**os.environ, 'PYTHONPATH': str(root / 'science/src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        program = '''
from pathlib import Path
from types import SimpleNamespace
import covscreen.ligand_chain as old
import covscreen_ligand_first.ligand_chain as new
from covscreen.source_release import frozen_release, verify_source, verify_input
from covscreen.layout import science_script
assert old is new
release = frozen_release()
spec = SimpleNamespace(provenance={'source_release': release})
verify_source(spec)
assert science_script('prepare_ligand_first_inputs.py').is_relative_to(Path(release['root']) / 'routes/ligand-first')
print(new.__file__)
'''
        probe = subprocess.run([sys.executable, '-B', '-c', program], env=env, cwd=self.root,
                               text=True, capture_output=True, timeout=30)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(Path(probe.stdout.strip()), root / 'routes/ligand-first/src/covscreen_ligand_first/ligand_chain.py')
        for location in ('routes/ligand-first/scripts', 'science/scripts'):
            probe = subprocess.run([sys.executable, '-B', str(root / location / 'compile_ligand_first_chain.py'), '--help'],
                env={**env, 'PYTHONPATH': ''}, cwd=self.root, text=True, capture_output=True, timeout=30)
            self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(self.install(), installed)
        changed = root / 'routes/ligand-first/scripts/prepare_ligand_first_inputs.py'
        changed.chmod(0o644)
        changed.write_text(changed.read_text() + '\n# tampered after approval\n')
        probe = subprocess.run([sys.executable, '-B', '-c', program], env=env, cwd=self.root,
                               text=True, capture_output=True, timeout=30)
        self.assertNotEqual(probe.returncode, 0)
        self.assertIn('Installed source release file changed', probe.stderr)

    def test_core_commands_use_installed_science_and_retain_shared_data_paths(self):
        installed = self.install()
        root = Path(installed['root'])
        spec = importlib.util.spec_from_file_location('released_core_worker', root / 'worker.py')
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        # Bind the real installed module to the temporary release parent. No
        # science engine or remote workload is invoked by these command checks.
        legacy = worker.DATA_PROJECT
        worker.ROOT = self.root
        worker.PROJECT, worker.RELEASE = worker.source_layout(root / 'worker.py', self.root, legacy)
        worker.TARGET = worker.PROJECT / 'configs/covindb/EGFR_C797_mvp_v1.yaml'
        files = worker.paths()
        self.assertEqual(worker.PROJECT, root / 'science')
        self.assertIn(root / 'manifest.json', files)
        self.assertIn(root / 'science/configs/local.inspire.yaml', files)
        self.assertTrue(all(root / name in files for name in installed['files']))
        self.assertEqual(worker.PDB, legacy / 'projects/egfr-c797/ligand_first/source_structures/1M17.pdb')
        self.assertEqual(worker.REFERENCE, legacy / 'projects/egfr-c797/ligand_first/derived/1M17_AQ4_parent_pose.sdf')
        plan = {'shortlist': 5, 'max_records': 2000, 'seed': 1}
        for phase, entry in [('prepare', 'run_covindb_prescreen.py'), ('carsidock', 'run_carsidock_registered.py'),
                             ('rtmscore', 'run_rtmscore_existing_poses.py')]:
            args = worker.command(plan, phase, self.root / 'tasks' / ('screen-' + 'a' * 24) / phase)
            expected_entry = root / ('routes/library-first/scripts' if phase == 'prepare' else 'science/scripts') / entry
            self.assertEqual(args[1], str(expected_entry))
            self.assertNotIn(str(legacy / 'scripts' / entry), args)
            if phase == 'prepare':
                self.assertEqual(args[args.index('--max-rows') + 1], '2000')
                self.assertEqual(args[args.index('--target-config') + 1], str(worker.TARGET))
            else:
                self.assertEqual(args[args.index('--pdb') + 1], str(worker.PDB))
        pilot = worker.command({'shortlist': 5, 'max_records': 20000, 'seed': 20260725}, 'prepare',
                               self.root / 'tasks' / ('screen-' + 'b' * 24) / 'prepare')
        self.assertEqual(pilot[pilot.index('--top-n') + 1], '5')
        self.assertEqual(pilot[pilot.index('--max-rows') + 1], '20000')
        self.assertEqual(pilot[pilot.index('--input') + 1], str(worker.SOURCE))
        # The source manifest is part of future frozen asset sets, and every
        # package file is checked before reading mutable-looking configuration.
        changed = root / 'science/configs/local.inspire.yaml'
        changed.chmod(0o644)
        changed.write_text('unexpected: replacement')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            worker.paths()

    def test_core_layout_does_not_silently_fall_back_from_invalid_release_name(self):
        spec = importlib.util.spec_from_file_location('core_layout_worker', Path(__file__).with_name('worker.py'))
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        legacy = self.root / 'legacy'
        self.assertEqual(worker.source_layout(self.root / 'worker.py', self.root, legacy), (legacy, None))
        with self.assertRaisesRegex(ValueError, 'Invalid core source release'):
            worker.source_layout(self.root / 'releases/not-an-id/worker.py', self.root, legacy)

    def test_changed_existing_release_is_not_overwritten_or_repaired(self):
        installed = self.install()
        entry = Path(installed['root']) / 'science/scripts/run_af3_registered_batch.py'
        entry.chmod(0o644)
        entry.write_text('modified source')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            self.install()
        self.assertEqual(entry.read_text(), 'modified source')

    def test_released_entrypoints_reject_changed_input_before_output_or_engine_work(self):
        installed = self.install()
        root = Path(installed['root'])
        env = {**os.environ, 'PYTHONPATH': str(root / 'science/src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        source = self.root / 'invalid.parquet'
        source.write_bytes(b'not a molecule table')
        for engine, extra in (
            ('af3', ['--source-data-json', '/not-used', '--target-config', '/not-used', '--runner', '/not-used',
                     '--af3-python', '/not-used', '--model-dir', '/not-used', '--database-dir', '/not-used',
                     '--cysteine-sequence-index', '105', '--target-id', 'EGFR', '--source-residue-number', '797']),
            ('covdock', ['--receptor', '/not-used', '--attachment-residue', 'A:773',
                         '--schrodinger-root', '/not-used', '--license-file', '/not-used']),
        ):
            entry = root / 'science/scripts' / ('run_af3_registered_batch.py' if engine == 'af3' else 'run_covdock_lo_registered.py')
            output = self.root / ('must-not-create-' + engine)
            probe = subprocess.run([sys.executable, '-B', str(entry), '--candidate-table', str(source),
                                    '--output-dir', str(output), '--expected-input-count', '1',
                                    '--expected-input-sha256', '0' * 64, '--expected-identity-sha256', '1' * 64, *extra],
                                   env=env, cwd=self.root, text=True, capture_output=True, timeout=30)
            self.assertNotEqual(probe.returncode, 0)
            self.assertIn('candidate file differs from the frozen subset', probe.stderr)
            self.assertFalse(output.exists())
        self.assertEqual(self.install(), installed)

    def test_extra_bytecode_and_symlinks_fail_closed(self):
        installed = self.install()
        root = Path(installed['root'])
        extra = root / 'science/src/covscreen/unrecorded.pyc'
        extra.write_bytes(b'not approved')
        with self.assertRaisesRegex(ValueError, 'Unrecorded'):
            self.install()
        extra.unlink()
        entry = root / 'science/scripts/run_af3_registered_batch.py'
        entry.unlink()
        entry.symlink_to(self.project / 'scripts/run_af3_registered_batch.py')
        with self.assertRaisesRegex(ValueError, 'nonregular'):
            self.install()

    def test_archive_escape_duplicate_and_symlink_rejected_before_install(self):
        for index, mode in enumerate(('escape', 'duplicate', 'symlink')):
            archive = self.root / f'invalid-{index}.tar.gz'
            with tarfile.open(archive, 'w:gz') as stream:
                name = '../outside.py' if mode == 'escape' else 'worker.py'
                info = tarfile.TarInfo(name)
                if mode == 'symlink':
                    info.type, info.linkname = tarfile.SYMTYPE, '/outside'
                    stream.addfile(info)
                else:
                    info.size = 4
                    stream.addfile(info, io.BytesIO(b'pass'))
                    if mode == 'duplicate':
                        stream.addfile(info, io.BytesIO(b'pass'))
            with self.assertRaisesRegex(ValueError, 'Archive includes'):
                code_release.install(archive, self.releases, self.package['release_id'], self.package['manifest_sha256'])
        self.assertEqual(list(self.releases.iterdir()), [])
        self.assertFalse((self.root / 'outside.py').exists())

    def test_wrong_manifest_or_release_identity_cannot_install(self):
        with self.assertRaisesRegex(ValueError, 'manifest differs'):
            code_release.install(self.package['archive'], self.releases, self.package['release_id'], '0' * 64)
        with self.assertRaisesRegex(ValueError, 'identity/content differs'):
            code_release.install(self.package['archive'], self.releases, '0' * 64, self.package['manifest_sha256'])
        self.assertEqual(list(self.releases.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
