"""Run the actual isolated bootstrap and staging worker against local file fixtures.

Only the activation environment is a fixture; no GPU, engine or platform call.
"""
import base64
import copy
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import code_release
import refinement_stage_test
import stage_bootstrap
import worker


class StageBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.source = refinement_stage_test.RefinementStageTest()
        self.source.setUp()
        self.addCleanup(self.source.tearDown)
        self.root = self.source.root
        project = self.source.source.root / 'fixture-science'
        shutil.copytree(Path(self.source.release['root']) / 'science', project)
        activation = project / 'environment/activate_carsidock.sh'
        activation.chmod(0o644)
        # If the bootstrap does not restore CPU settings after activation, the
        # shell's DEBUG trap rejects the child before any staging is performed.
        activation.write_text('export CUDA_VISIBLE_DEVICES=fixture-gpu JAX_PLATFORMS=cuda\n'
            'export OMP_NUM_THREADS=99 OPENBLAS_NUM_THREADS=99 MKL_NUM_THREADS=99\n'
            'trap \'if [[ "$BASH_COMMAND" == \'"\'"\'exec "$@"\'"\'"\' ]]; then '
            '[[ -z "$CUDA_VISIBLE_DEVICES" && "$JAX_PLATFORMS" == cpu && '
            '"$OMP_NUM_THREADS:$OPENBLAS_NUM_THREADS:$MKL_NUM_THREADS" == 1:1:1 ]] || exit 97; fi\' DEBUG\n')
        package = code_release.build(project, self.source.source.root)
        release = code_release.install(package['archive'], self.root / 'releases',
                                       package['release_id'], package['manifest_sha256'])
        self.plan = copy.deepcopy(self.source.plan)
        self.plan['code'] = {key: release[key] for key in ('release_id', 'manifest_sha256')}
        self.plan['assets'] = {str(Path(release['root']) / name): checksum
                               for name, checksum in release['files'].items()}
        self.release = Path(release['root'])
        self.packet = {'root': str(self.root), 'release': release['release_id'],
            'manifest': release['manifest_sha256'], 'checker_sha256': release['files']['code_release.py'],
            'python': sys.executable, 'python_sha256': code_release.sha(Path(sys.executable).read_bytes()),
            'action': 'freeze', 'digest': worker.digest(self.plan), 'parent': self.plan['parent_id'],
            'task': self.plan['id'], 'payload': base64.b64encode(json.dumps(self.plan).encode()).decode()}

    def test_actual_isolated_bootstrap_freezes_and_reads_exact_plan_cpu_only(self):
        command = [sys.executable, '-I', '-B', '-c', Path(stage_bootstrap.__file__).read_text()]
        result = subprocess.run([*command, base64.b64encode(json.dumps(self.packet).encode()).decode()],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        frozen = json.loads(result.stdout)
        read = stage_bootstrap.execute({**self.packet, 'action': 'read', 'payload': None})
        self.assertEqual(read, frozen)
        self.assertEqual(frozen['digest'], worker.digest(self.plan))
        self.assertFalse(frozen['runtime_verified'])
        self.assertFalse(frozen['resource_approval_granted'])
        self.assertFalse(Path(frozen['output']).exists())

    def test_changed_checker_is_rejected_before_its_bytes_execute(self):
        marker = self.root / 'untrusted-executed'
        checker = self.release / 'code_release.py'
        checker.chmod(0o644)
        checker.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        with self.assertRaisesRegex(ValueError, 'no release code executed'):
            stage_bootstrap.execute(self.packet)
        self.assertFalse(marker.exists())

    def test_release_and_interpreter_changes_rejected_before_activation(self):
        with self.assertRaisesRegex(ValueError, 'interpreter changed'):
            stage_bootstrap.execute({**self.packet, 'python_sha256': '0' * 64})
        activation = self.release / 'science/environment/activate_carsidock.sh'
        activation.chmod(0o644)
        marker = self.root / 'activation-executed'
        activation.write_text('touch ' + str(marker) + '\n')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            stage_bootstrap.execute(self.packet)
        self.assertFalse(marker.exists())

    def test_release_parent_symlink_cannot_redirect_verified_code(self):
        releases = self.root / 'releases'
        moved = self.root / 'moved-releases'
        releases.rename(moved)
        releases.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'canonical execution root'):
            stage_bootstrap.execute(self.packet)

    def test_read_cannot_replace_plan_and_freeze_requires_bound_identity(self):
        for packet in ({**self.packet, 'action': 'read'},
                       {**self.packet, 'task': 'refine-' + 'b' * 24},
                       {**self.packet, 'action': 'submit'},
                       {**self.packet, 'extra': True}):
            with self.assertRaises(ValueError):
                stage_bootstrap.execute(packet)
        self.assertFalse((self.root / 'tasks' / self.plan['parent_id'] / 'refinements').exists())


if __name__ == '__main__':
    unittest.main()
