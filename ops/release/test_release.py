import importlib.util
import json
from pathlib import Path
import tempfile
import io
import tarfile
import unittest
import subprocess
import sys
import os
import configparser

spec = importlib.util.spec_from_file_location('release', Path(__file__).with_name('release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_platform_science_and_release_regressions_are_registered(self):
        root = Path(__file__).resolve().parents[2]
        registry = json.loads((root / 'ops/release/components.json').read_text())
        selected = set(registry['source_files'])
        source_roots = [Path(value) for value in registry['source_roots']]
        required = list((root / 'science/src').rglob('*.py'))
        required += [path for path in (root / 'ops').rglob('*')
                     if path.suffix in {'.py', '.ts', '.sh'} and '__pycache__' not in path.parts]
        required += list((root / 'science/src/covscreen/data').glob('*'))
        config = configparser.ConfigParser()
        config.read(root / 'pytest-ligand-release.ini')
        required += [root / value for value in config['pytest']['testpaths'].split()]
        for filename in required:
            relative = filename.relative_to(root)
            with self.subTest(file=str(relative)):
                self.assertTrue(filename.is_file(), 'Declared regression file is missing')
                self.assertTrue(str(relative) in selected or any(
                    parent == relative or parent in relative.parents for parent in source_roots
                ), 'Source or regression missing from release inventory')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'ops/release').mkdir(parents=True)
        (self.root / 'src').mkdir()
        self.registry = {'source_roots': ['src'], 'source_files': []}
        (self.root / 'ops/release/components.json').write_text(json.dumps(self.registry))
        (self.root / 'src/app.py').write_text('print(1)\n')

    def test_repeatable_and_verified(self):
        a = release.build(self.root, self.root / 'a')
        b = release.build(self.root, self.root / 'b')
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(release.verify(a)['files'], 1)
        self.assertEqual(release.build(self.root, self.root / 'a'), a)

    def test_registered_dispatchers_import_from_isolated_package(self):
        root = Path(__file__).resolve().parents[2]
        registry = json.loads((root / 'ops/release/components.json').read_text())
        files = release.collect(root, registry)
        # Materialize only the packaged ops files, never import from the checkout.
        for name, raw in files.items():
            if name.startswith('routes/ligand-first/ops/'):
                target = self.root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
        ops = self.root / 'routes/ligand-first/ops'
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        env.pop('PYTHONPATH', None)
        result = subprocess.run([sys.executable, '-I', '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); '
            'import dispatch_ligand_lo, dispatch_ligand_vs; '
            'assert "launch_observation" in dispatch_ligand_lo.hpc.RELEASE_LAUNCH', str(ops)],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_excludes_runtime_and_auth(self):
        (self.root / 'src/node_modules').mkdir()
        (self.root / 'src/node_modules/a.js').write_text('ignored')
        (self.root / 'src/live-web-assets').mkdir()
        (self.root / 'src/live-web-assets/generated.js').write_text(
            'sk-' + 'z' * 30
        )
        (self.root / 'src/auth.json').write_text('ignored')
        (self.root / 'src/.env').write_text('ignored')
        (self.root / 'src/.runtime-generated.mjs').write_text('generated')
        (self.root / 'src/dist-workbench').mkdir()
        (self.root / 'src/dist-workbench/index.js').write_text('generated')
        self.assertEqual(list(release.collect(self.root, self.registry)), ['src/app.py'])

    def test_generated_executor_cannot_reenter_explicit_source_inventory(self):
        (self.root / 'src/.runtime-generated.mjs').write_text('generated')
        self.registry['source_files'] = ['src/.runtime-generated.mjs']
        with self.assertRaisesRegex(ValueError, 'Generated runtime file'):
            release.collect(self.root, self.registry)

    def test_explicit_skill_root_is_packaged(self):
        skill = self.root / 'skills/site-discovery/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('# Site discovery\n')
        self.registry['source_roots'].append('skills')
        self.assertEqual(
            sorted(release.collect(self.root, self.registry)),
            ['skills/site-discovery/SKILL.md', 'src/app.py'],
        )

    def test_explicit_exclusion_does_not_disable_credential_scanning(self):
        (self.root / 'src/private-notes.md').write_text('sk-' + 'z' * 30)
        self.registry['source_exclude'] = ['src/private-notes.md']
        self.assertEqual(list(release.collect(self.root, self.registry)), ['src/app.py'])
        (self.root / 'src/app.py').write_text('sk-' + 'x' * 30)
        with self.assertRaisesRegex(ValueError, 'Credential-like'):
            release.collect(self.root, self.registry)
        self.registry['source_exclude'] = ['../outside']
        with self.assertRaisesRegex(ValueError, 'Invalid excluded'):
            release.collect(self.root, self.registry)

    def test_rejects_secret_and_missing_file(self):
        (self.root / 'src/app.py').write_text('sk-' + 'z' * 30)
        with self.assertRaisesRegex(ValueError, 'Credential-like'):
            release.collect(self.root, self.registry)
        (self.root / 'src/app.py').write_text('print(1)')
        self.registry['source_files'] = ['missing.py']
        with self.assertRaises(ValueError):
            release.collect(self.root, self.registry)

    def test_change_produces_new_version(self):
        a = release.build(self.root, self.root / 'out')
        (self.root / 'src/app.py').write_text('print(2)')
        b = release.build(self.root, self.root / 'out')
        self.assertNotEqual(a.name, b.name)
        self.assertEqual(release.verify(a)['files'], 1)

    def test_unsafe_paths(self):
        for path in ('/tmp/a', '../a', 'a/../b', 'a\\b'):
            self.assertFalse(release.safe(path))

    def test_symlinks_are_not_followed(self):
        (self.root / 'private.py').write_text('not source')
        (self.root / 'src/link.py').symlink_to(self.root / 'private.py')
        self.assertEqual(list(release.collect(self.root, self.registry)), ['src/app.py'])
        self.registry['source_files'] = ['src/link.py']
        with self.assertRaises(ValueError):
            release.collect(self.root, self.registry)

    def test_corruption_rejected(self):
        archive = release.build(self.root, self.root / 'out')
        with tarfile.open(archive) as tar:
            files = {entry.name: tar.extractfile(entry).read() for entry in tar}
        files['src/app.py'] = b'changed'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
        with self.assertRaisesRegex(ValueError, 'hashes differ'):
            release.verify(archive)

    def test_registered_internal_asset_link_is_materialized(self):
        (self.root / 'src/alias.py').symlink_to('app.py')
        self.registry['source_links'] = {'src/alias.py': 'src/app.py'}
        contents = release.collect(self.root, self.registry)
        self.assertEqual(contents['src/alias.py'], contents['src/app.py'])
        (self.root / 'src/alias.py').unlink()
        (self.root / 'outside.py').write_text('unregistered')
        (self.root / 'src/alias.py').symlink_to('../outside.py')
        with self.assertRaisesRegex(ValueError, 'link changed'):
            release.collect(self.root, self.registry)

    def test_registered_link_cannot_add_an_unselected_target(self):
        self.registry['source_links'] = {'src/alias.py': '../outside.py'}
        with self.assertRaisesRegex(ValueError, 'Invalid registered'):
            release.collect(self.root, self.registry)


if __name__ == '__main__':
    unittest.main()
